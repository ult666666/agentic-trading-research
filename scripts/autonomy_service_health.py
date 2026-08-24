#!/usr/bin/env python3
"""Multi-plane autonomy health reconciler (read-only observability).

Separates scheduler/configuration health from workflow outcome health and the Morning service
deadline. A loaded, byte-matching LaunchAgent can therefore remain configuration-healthy while a
failed-safe workflow and missed 06:15 PT terminal SLO are reported independently.

This module reads status artifacts only. It cannot schedule work, invoke a model or broker, alter
a report, grant Buy authority, or change strategy/risk policy. Std-lib only.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import launchd_scheduler_health as scheduler_health
import autonomy_orchestrator as orchestrator
import broker_runtime_audit
import free_source_health as source_health
import free_source_health_alert as source_alert
import power_schedule_health as power_health
import release_snapshot
import runtime_provenance
import validate_market_day
import candidate_quote_hydrator
import deterministic_no_live_terminal
import freeze_completion_probe
import macro_actual_refresh
import post_close_finality
import post_close_maintenance_receipt
import post_open_finality

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "autonomy_service_health/1.0"
STATE_SCHEMA = "autonomy_daily_state/1.1"
PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
TARGET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,180}$")
RUNTIME_AUTHORITY_BINDINGS = frozenset({"match", "expected_output_drift"})
WORKFLOWS = ("intake", "macro_refresh", "morning", "post_open", "post_close")
EXPECTED_SKIP_OUTCOMES = {"no_official_morning_artifact", "non_trading_day"}
SERVICE_SLO_POLICY_PATH = ROOT / "config" / "autonomy_service_slos.json"
SERVICE_SLO_POLICY_SCHEMA = "autonomy_service_slo_policy/1.0"
SERVICE_SLO_POLICY_KEYS = {
    "schema", "version", "operating_timezone", "market_calendar_required",
    "workflows", "safety",
}
SERVICE_SLO_WORKFLOW_KEYS = {
    "scheduled_pt", "start_deadline_pt", "terminal_deadline_pt", "objective",
    "terminal_source", "accepted_terminal_workflow_statuses",
    "expected_skip_outcomes",
}
SERVICE_SLO_SAFETY = {
    "read_only": True,
    "diagnostic_only": True,
    "local_artifact_only": True,
    "retry_authority": False,
    "schedule_change_allowed": False,
    "report_change_allowed": False,
    "strategy_change_allowed": False,
    "execution_enabled": False,
    "broker_invoked": False,
    "contains_account_data": False,
    "contains_credentials": False,
}
EXPECTED_SERVICE_SLOS = {
    "intake": {
        "scheduled_pt": "05:00",
        "start_deadline_pt": "05:05",
        "terminal_deadline_pt": "05:20",
        "objective": "validated_data_readiness_terminal",
        "terminal_source": "autonomy_attempt",
        "accepted_terminal_workflow_statuses": ["degraded", "healthy"],
        "expected_skip_outcomes": [],
    },
    "macro_refresh": {
        "scheduled_pt": "05:35",
        "start_deadline_pt": "05:40",
        "terminal_deadline_pt": "05:45",
        "objective": "validated_macro_actual_refresh_terminal",
        "terminal_source": "autonomy_attempt",
        "accepted_terminal_workflow_statuses": ["degraded", "healthy"],
        "expected_skip_outcomes": [],
    },
    "morning": {
        "scheduled_pt": "05:38",
        "start_deadline_pt": "05:43",
        "terminal_deadline_pt": "06:15",
        "objective": "validated_canonical_morning_terminal",
        "terminal_source": "validated_morning_terminal",
        "accepted_terminal_workflow_statuses": [],
        "expected_skip_outcomes": [],
    },
    "post_open": {
        "scheduled_pt": "06:45",
        "start_deadline_pt": "06:50",
        "terminal_deadline_pt": "07:05",
        "objective": "validated_post_open_append_or_expected_skip",
        "terminal_source": "autonomy_attempt",
        "accepted_terminal_workflow_statuses": [
            "degraded", "healthy", "skipped_expected"],
        "expected_skip_outcomes": ["no_official_morning_artifact"],
    },
    "post_close": {
        "scheduled_pt": "13:15",
        "start_deadline_pt": "13:20",
        "terminal_deadline_pt": "14:15",
        "objective": "validated_post_close_report_or_maintenance_terminal",
        "terminal_source": "autonomy_attempt",
        # Exact Morning absence is accepted only after the immutable deterministic-maintenance
        # receipt revalidates. A bare skipped state never meets this objective.
        # config/autonomy_service_slos.json is dual-pinned against this constant by exact
        # equality, so the JSON and this dict must always be changed together.
        "accepted_terminal_workflow_statuses": [
            "degraded", "healthy", "skipped_expected"],
        "expected_skip_outcomes": ["no_official_morning_artifact"],
    },
}
SAFETY = {
    "read_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "contains_account_data": False,
    "contains_credentials": False,
}
RESEARCH_ARTIFACT_SAFETY = {
    "read_only": True,
    "diagnostic_only": True,
    "production_gate_enforced": False,
    "execution_enabled": False,
    "activation_authority": False,
}
STATE_KEYS = {
    "schema", "target_session_date", "operating_timezone", "research_only",
    "execution_enabled", "activation_authority", "strategy_change_allowed",
    "config_sha256", "revision", "overall_state", "workflows", "events",
    "updated_utc",
}
STATE_SAFETY = {
    "research_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
}
STATE_WORKFLOW_KEYS = {"state", "latest_run_id", "attempts"}
STATE_ATTEMPT_KEYS = {
    "run_id", "config_sha256", "state", "outcome", "dry_run", "owner_pid",
    "child_pid", "started_utc", "finished_utc", "exit_code", "preflight",
    "postconditions",
}
STATE_EVENT_KEYS = {"seq", "utc", "workflow", "run_id", "event", "reason"}
WORKFLOW_STATES = {
    "pending", "running", "completed", "degraded", "skipped", "failed", "blocked",
}
TERMINAL_WORKFLOW_STATES = {
    "completed", "degraded", "skipped", "failed", "blocked",
}
OVERALL_STATES = {"pending", "running", "in_progress", "complete", "at_issue"}
WORKFLOW_HEALTH_KEYS = {
    "status", "state", "reason", "latest_run_id", "outcome", "exit_code",
    "started_utc", "finished_utc",
}
#: First target date whose post_close skip can possibly carry a maintenance receipt.  Sessions
#: that already terminated under the previous contract can never acquire one, so demanding it
#: retroactively would mark 11 settled days — including 2026-08-17, which is already terminal —
#: permanently `at_issue`.  ISO dates compare lexicographically.
MAINTENANCE_RECEIPT_EFFECTIVE_DATE = "2026-08-18"

WORKFLOW_HEALTH_STATUSES = {
    "healthy", "degraded", "skipped_expected", "skipped", "failed_safe",
    "failed", "blocked", "pending", "running", "unknown", "at_issue",
}


def _time_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _load_service_slo_policy(
        path: Path = SERVICE_SLO_POLICY_PATH) -> tuple[dict | None, list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["service_slo_policy_unreadable"]
    errors = validate_service_slo_policy(document)
    return (document if not errors else document), errors


def validate_service_slo_policy(document: Any,
                                workflow_config: dict | None = None) -> list[str]:
    if not isinstance(document, dict):
        return ["service_slo_policy_not_object"]
    errors: list[str] = []
    if set(document) != SERVICE_SLO_POLICY_KEYS:
        errors.append("service_slo_policy_keys_invalid")
    if (document.get("schema") != SERVICE_SLO_POLICY_SCHEMA
            or document.get("version") != "1.0.0"):
        errors.append("service_slo_policy_version_invalid")
    if document.get("operating_timezone") != "America/Los_Angeles":
        errors.append("service_slo_policy_timezone_invalid")
    if document.get("market_calendar_required") is not True:
        errors.append("service_slo_market_calendar_not_required")
    if document.get("safety") != SERVICE_SLO_SAFETY:
        errors.append("service_slo_policy_safety_invalid")
    workflows = document.get("workflows")
    if not isinstance(workflows, dict) or set(workflows) != set(WORKFLOWS):
        errors.append("service_slo_policy_workflows_invalid")
        workflows = {}
    if workflow_config is None:
        try:
            workflow_config = json.loads(
                (ROOT / "config" / "autonomy_workflows.json").read_text(
                    encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            workflow_config = None
    configured = (
        workflow_config.get("workflows")
        if isinstance(workflow_config, dict) else None)
    if not isinstance(configured, dict):
        errors.append("service_slo_autonomy_config_unavailable")
        configured = {}
    for name in WORKFLOWS:
        row = workflows.get(name)
        if not isinstance(row, dict) or set(row) != SERVICE_SLO_WORKFLOW_KEYS:
            errors.append(f"service_slo_workflow_shape_invalid:{name}")
            continue
        if row != EXPECTED_SERVICE_SLOS[name]:
            errors.append(f"service_slo_workflow_contract_invalid:{name}")
        scheduled = _time_minutes(row.get("scheduled_pt"))
        start = _time_minutes(row.get("start_deadline_pt"))
        terminal = _time_minutes(row.get("terminal_deadline_pt"))
        if scheduled is None or start is None or terminal is None \
                or not scheduled < start < terminal:
            errors.append(f"service_slo_deadline_order_invalid:{name}")
        elif start - scheduled != 5:
            errors.append(f"service_slo_start_grace_invalid:{name}")
        configured_row = configured.get(name)
        if (not isinstance(configured_row, dict)
                or configured_row.get("scheduled_pt") != row.get("scheduled_pt")):
            errors.append(f"service_slo_schedule_mismatch:{name}")
        for key in (
                "accepted_terminal_workflow_statuses",
                "expected_skip_outcomes"):
            values = row.get(key)
            if (not isinstance(values, list)
                    or values != sorted(set(values))
                    or any(not isinstance(value, str) or not value
                           for value in values)):
                errors.append(f"service_slo_list_invalid:{name}:{key}")
    return sorted(set(errors))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _payload(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "artifact_sha256"}


def _sha_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _target_date(target: str) -> dt.date:
    if not TARGET_RE.fullmatch(target):
        raise ValueError("target_invalid")
    parsed = dt.date.fromisoformat(target)
    if parsed.isoformat() != target:
        raise ValueError("target_invalid")
    return parsed


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_freeze_receipt(
        receipt: dict | None, target: str, root: Path,
        now: dt.datetime | None = None) -> tuple[dt.datetime | None, list[str]]:
    """Validate one v2 Morning terminal without granting authority from a report hash.

    Preliminary receipts retain their diagnostic terminal contract.  Any Official/Buy claim is
    accepted only when the exact canonical receipt also passes the shared Official-only probe,
    which revalidates evidence, live-source bytes, research authority, candidate quotes, prose,
    title, and the canonical report.
    """
    if receipt is None:
        return None, ["freeze_receipt_missing_or_unreadable"]
    errors: list[str] = []
    if receipt.get("schema") != "research_freeze_receipt/1.0":
        errors.append("freeze_receipt_schema_invalid")
    if receipt.get("target_session_date") != target:
        errors.append("freeze_receipt_target_mismatch")
    frozen = _parse_timestamp(receipt.get("frozen_utc"))
    if frozen is None:
        errors.append("freeze_timestamp_invalid")
    raw_path = receipt.get("report_path")
    report_path = Path(raw_path) if isinstance(raw_path, str) and raw_path else None
    if report_path is not None and not report_path.is_absolute():
        report_path = root / report_path
    if report_path is None or not _inside(report_path, root):
        errors.append("freeze_report_path_invalid")
    elif not report_path.is_file():
        errors.append("freeze_report_missing")
    elif _sha_file(report_path) != receipt.get("report_sha256"):
        errors.append("freeze_report_sha256_mismatch")
    if receipt.get("execution_enabled") is not False:
        errors.append("freeze_execution_flag_invalid")
    classification = str(receipt.get("classification") or "")
    authority_claim = bool(
        receipt.get("official") is True
        or receipt.get("official_research") is True
        or receipt.get("buy_authorized") is True
        or classification.startswith("OFFICIAL_RESEARCH_")
    )
    if authority_claim:
        canonical_dir = root / "data" / "runtime" / "research" / target
        canonical_receipt = canonical_dir / "freeze_receipt.json"
        try:
            on_disk = json.loads(canonical_receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            on_disk = None
            errors.append("freeze_canonical_receipt_missing_or_unreadable")
        if (canonical_receipt.is_symlink() or canonical_dir.is_symlink()
                or on_disk != receipt):
            errors.append("freeze_canonical_receipt_mismatch")
        else:
            probe_now = now or dt.datetime.now(UTC)
            if probe_now.tzinfo is None:
                probe_now = probe_now.replace(tzinfo=UTC)
            probe = freeze_completion_probe.probe(
                target,
                canonical_dir,
                root / "reports",
                now=probe_now,
                official_only=True,
            )
            if (probe.get("completed") is not True
                    or probe.get("kind") != "research_freeze_receipt"
                    or probe.get("official") is not True):
                errors.append(
                    "freeze_official_reanchor_failed:"
                    + str(probe.get("reason") or "unknown"))
            try:
                after_probe = json.loads(
                    canonical_receipt.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                after_probe = None
            if canonical_receipt.is_symlink() or after_probe != receipt:
                errors.append("freeze_canonical_receipt_changed_during_reanchor")
    return (frozen if not errors else None), errors


def _validate_deterministic_terminal(target: str, root: Path) \
        -> tuple[dt.datetime | None, list[str]]:
    """Validate the one active deterministic no-live terminal, never bare report presence."""
    receipt_dir = root / "data" / "runtime" / "research" / target / "deterministic_terminals"
    if not receipt_dir.is_dir() or receipt_dir.is_symlink():
        return None, ["deterministic_terminal_receipt_missing_or_unreadable"]
    valid: list[dt.datetime] = []
    errors: list[str] = []
    expected_preflight = (root / "data" / "runtime" / "preflight" / f"{target}.json").resolve()
    for path in sorted(receipt_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file() or not _inside(path, receipt_dir):
            errors.append("deterministic_terminal_receipt_path_invalid")
            continue
        receipt = _read_json(path)
        candidate_errors = deterministic_no_live_terminal.validate_receipt(path, root)
        if not isinstance(receipt, dict):
            candidate_errors.append("deterministic_terminal_receipt_unreadable")
        else:
            if receipt.get("target_session_date") != target:
                candidate_errors.append("deterministic_terminal_target_mismatch")
            if receipt.get("classification") != "PRELIMINARY_PRICE_UNAVAILABLE":
                candidate_errors.append("deterministic_terminal_classification_invalid")
            if receipt.get("workflow_stage") not in (
                    deterministic_no_live_terminal.ALLOWED_STAGES):
                candidate_errors.append("deterministic_terminal_stage_invalid")
            if receipt.get("preflight_artifact_valid") is not True:
                candidate_errors.append("deterministic_terminal_preflight_flag_invalid")
            preflight_raw = receipt.get("preflight_path")
            preflight = Path(preflight_raw) if isinstance(preflight_raw, str) else None
            if preflight is not None and not preflight.is_absolute():
                preflight = root / preflight
            if (preflight is None or not _inside(preflight, root)
                    or preflight.resolve() != expected_preflight
                    or _sha_file(preflight) != receipt.get("preflight_sha256")):
                candidate_errors.append("deterministic_terminal_preflight_hash_mismatch")
            report_raw = receipt.get("report_path")
            report = Path(report_raw) if isinstance(report_raw, str) else None
            if report is not None and not report.is_absolute():
                report = root / report
            preliminary_root = (root / "reports" / "morning" / "preliminary").resolve()
            if report is None or not _inside(report, preliminary_root):
                candidate_errors.append("deterministic_terminal_report_path_invalid")
            created = _parse_timestamp(receipt.get("created_utc"))
            if created is None:
                candidate_errors.append("deterministic_terminal_timestamp_invalid")
            else:
                created_pt = created.astimezone(PT)
                minute = created_pt.hour * 60 + created_pt.minute
                if created_pt.date().isoformat() != target or not 345 <= minute <= 380:
                    candidate_errors.append("deterministic_terminal_window_invalid")
        if candidate_errors:
            errors.extend(candidate_errors)
        else:
            valid.append(created)
    if len(valid) > 1:
        return None, sorted(set(errors + ["deterministic_terminal_receipt_ambiguous"]))
    if len(valid) == 1:
        return valid[0], sorted(set(errors))
    return None, sorted(set(errors or [
        "deterministic_terminal_receipt_missing_or_unreadable"]))


def _scheduler_plane(document: dict | None) -> dict:
    if not isinstance(document, dict):
        return {"status": "unavailable", "ok": False, "labels": {},
                "reason": "scheduler_health_missing_or_unreadable"}
    labels: dict[str, str] = {}
    for entry in document.get("labels") or []:
        if isinstance(entry, dict) and isinstance(entry.get("label"), str):
            labels[entry["label"]] = str(
                entry.get("scheduler_config_status") or entry.get("status") or "unknown")
    config_ok = document.get("scheduler_config_ok")
    if config_ok is None:
        config_ok = document.get("ok") is True and bool(labels) and all(
            value == "healthy" for value in labels.values())
    status = "healthy" if config_ok is True else "at_issue"
    return {
        "status": status,
        "ok": status == "healthy",
        "scope": "scheduler_configuration_only",
        "labels": dict(sorted(labels.items())),
        "reason": "loaded_repository_installed_config_agree" if status == "healthy"
                  else "scheduler_configuration_not_fully_healthy",
    }


def _state_timestamp(value: Any) -> dt.datetime | None:
    """Aware timestamp parser used only for autonomy-state/order validation."""
    return _parse_timestamp(value)


def _valid_run_id(value: Any) -> bool:
    return isinstance(value, str) and SAFE_TOKEN_RE.fullmatch(value) is not None


def _integer(value: Any, *, minimum: int | None = None) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and (minimum is None or value >= minimum))


def _macro_history_errors(document: dict, target: str) -> list[str]:
    """Allow at most one exact watchdog-no-child -> Morning recovery handoff."""
    workflows = document.get("workflows")
    workflow = workflows.get("macro_refresh") if isinstance(workflows, dict) else None
    attempts = workflow.get("attempts") if isinstance(workflow, dict) else None
    if not isinstance(attempts, list) or len(attempts) != 2:
        return []
    first, second = attempts
    if not isinstance(first, dict) or not isinstance(second, dict):
        return ["autonomy_state_macro_handoff_invalid"]
    second_started = _state_timestamp(second.get("started_utc"))
    if second_started is None:
        return ["autonomy_state_macro_handoff_invalid"]

    first_only = copy.deepcopy(document)
    first_workflow = first_only["workflows"]["macro_refresh"]
    first_workflow["attempts"] = [copy.deepcopy(first)]
    first_workflow["state"] = first.get("state")
    first_workflow["latest_run_id"] = first.get("run_id")
    first_only["events"] = [
        copy.deepcopy(event) for event in document.get("events", [])
        if not (isinstance(event, dict)
                and event.get("workflow") == "macro_refresh"
                and event.get("run_id") != first.get("run_id"))
    ]
    for index, event in enumerate(first_only["events"]):
        if isinstance(event, dict):
            event["seq"] = index
    proven = orchestrator._watchdog_no_dispatch_handoff_run_id(
        first_only, target, second_started, str(document.get("config_sha256") or ""))
    preflight = second.get("preflight")
    required = {
        "continuity_recovery": True,
        "recovery_trigger": "morning",
        "recovery_only_if_never_started": True,
        "recovery_child_timeout_seconds": orchestrator.MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS,
        "watchdog_no_dispatch_handoff": True,
        "handoff_from_run_id": first.get("run_id"),
    }
    if (proven != first.get("run_id")
            or second.get("config_sha256") != first.get("config_sha256")
            or not isinstance(preflight, dict)
            or any(preflight.get(key) != value for key, value in required.items())):
        return ["autonomy_state_macro_handoff_invalid"]
    return []


def _valid_state(document: dict | None, target: str) -> tuple[dict | None, list[str]]:
    """Validate the complete autonomy state before projecting any workflow as healthy.

    This intentionally mirrors the state shape written by autonomy_orchestrator.  A malformed
    record invalidates the entire workflow plane; partial trust would let a stale top-level state,
    forged latest pointer, or duplicate run identity appear authoritative.
    """
    if not isinstance(document, dict):
        return None, ["autonomy_state_missing_or_unreadable"]
    errors: list[str] = []
    if set(document) != STATE_KEYS:
        errors.append("autonomy_state_keys_invalid")
    if document.get("schema") != STATE_SCHEMA:
        errors.append("autonomy_state_schema_invalid")
    if document.get("target_session_date") != target:
        errors.append("autonomy_state_target_mismatch")
    if document.get("operating_timezone") != "America/Los_Angeles":
        errors.append("autonomy_state_timezone_invalid")
    for key, expected in STATE_SAFETY.items():
        if document.get(key) is not expected:
            errors.append(f"autonomy_state_safety_invalid:{key}")
    if not isinstance(document.get("config_sha256"), str) \
            or SHA256_RE.fullmatch(document["config_sha256"]) is None:
        errors.append("autonomy_state_config_sha256_invalid")
    if not _integer(document.get("revision"), minimum=0):
        errors.append("autonomy_state_revision_invalid")
    if document.get("overall_state") not in OVERALL_STATES:
        errors.append("autonomy_state_overall_state_invalid")
    updated = _state_timestamp(document.get("updated_utc"))
    if updated is None:
        errors.append("autonomy_state_updated_utc_invalid")

    workflows = document.get("workflows")
    if not isinstance(workflows, dict) or set(workflows) != set(WORKFLOWS):
        errors.append("autonomy_state_workflows_invalid")
        workflows = {}

    seen_run_ids: set[str] = set()
    run_workflows: dict[str, str] = {}
    attempt_records: list[tuple[str, int, dict]] = []
    observed_times: list[dt.datetime] = []
    for name in WORKFLOWS:
        workflow = workflows.get(name)
        if not isinstance(workflow, dict) or set(workflow) != STATE_WORKFLOW_KEYS:
            errors.append(f"autonomy_state_workflow_shape_invalid:{name}")
            continue
        state_value = workflow.get("state")
        if state_value not in WORKFLOW_STATES:
            errors.append(f"autonomy_state_workflow_state_invalid:{name}")
        attempts = workflow.get("attempts")
        if not isinstance(attempts, list):
            errors.append(f"autonomy_state_attempts_invalid:{name}")
            continue
        if name == "macro_refresh" and len(attempts) > 2:
            errors.append("autonomy_state_macro_attempt_limit_exceeded")
        if name == "post_close" and len(attempts) > 1:
            errors.append("autonomy_state_post_close_attempt_limit_exceeded")
        if not attempts:
            if state_value != "pending":
                errors.append(f"autonomy_state_empty_attempt_state_invalid:{name}")
            if workflow.get("latest_run_id") is not None:
                errors.append(f"autonomy_state_empty_attempt_latest_run_id_invalid:{name}")
            continue

        prior_started: dt.datetime | None = None
        prior_finished: dt.datetime | None = None
        for index, attempt in enumerate(attempts):
            label = f"{name}:{index}"
            if not isinstance(attempt, dict) or set(attempt) != STATE_ATTEMPT_KEYS:
                errors.append(f"autonomy_state_attempt_shape_invalid:{label}")
                continue
            run_id = attempt.get("run_id")
            if not _valid_run_id(run_id):
                errors.append(f"autonomy_state_attempt_run_id_invalid:{label}")
            elif run_id in seen_run_ids:
                errors.append(f"autonomy_state_duplicate_run_id:{run_id}")
            else:
                seen_run_ids.add(run_id)
                run_workflows[run_id] = name
            attempt_records.append((name, index, attempt))
            config_sha = attempt.get("config_sha256")
            if not isinstance(config_sha, str) or SHA256_RE.fullmatch(config_sha) is None:
                errors.append(f"autonomy_state_attempt_config_sha256_invalid:{label}")
            attempt_state = attempt.get("state")
            if attempt_state not in WORKFLOW_STATES - {"pending"}:
                errors.append(f"autonomy_state_attempt_state_invalid:{label}")
            if attempt.get("dry_run") not in (True, False) \
                    or not isinstance(attempt.get("dry_run"), bool):
                errors.append(f"autonomy_state_attempt_dry_run_invalid:{label}")
            if not _integer(attempt.get("owner_pid"), minimum=2):
                errors.append(f"autonomy_state_attempt_owner_pid_invalid:{label}")
            child_pid = attempt.get("child_pid")
            if child_pid is not None and not _integer(child_pid, minimum=2):
                errors.append(f"autonomy_state_attempt_child_pid_invalid:{label}")
            if not isinstance(attempt.get("preflight"), dict):
                errors.append(f"autonomy_state_attempt_preflight_invalid:{label}")
            if attempt.get("postconditions") is not None \
                    and not isinstance(attempt.get("postconditions"), dict):
                errors.append(f"autonomy_state_attempt_postconditions_invalid:{label}")

            started = _state_timestamp(attempt.get("started_utc"))
            finished = _state_timestamp(attempt.get("finished_utc"))
            if started is None:
                errors.append(f"autonomy_state_attempt_started_utc_invalid:{label}")
            else:
                observed_times.append(started)
                if prior_started is not None and started < prior_started:
                    errors.append(f"autonomy_state_attempt_start_order_invalid:{label}")
                if prior_finished is not None and started < prior_finished:
                    errors.append(f"autonomy_state_attempt_overlap_invalid:{label}")
            if attempt_state == "running":
                if (attempt.get("outcome") is not None
                        or attempt.get("finished_utc") is not None
                        or attempt.get("exit_code") is not None):
                    errors.append(f"autonomy_state_running_attempt_terminal_fields:{label}")
            elif attempt_state in TERMINAL_WORKFLOW_STATES:
                outcome = attempt.get("outcome")
                if not isinstance(outcome, str) or SAFE_TOKEN_RE.fullmatch(outcome) is None:
                    errors.append(f"autonomy_state_attempt_outcome_invalid:{label}")
                if finished is None:
                    errors.append(f"autonomy_state_attempt_finished_utc_invalid:{label}")
                else:
                    observed_times.append(finished)
                    if started is not None and finished < started:
                        errors.append(f"autonomy_state_attempt_finished_before_start:{label}")
                exit_code = attempt.get("exit_code")
                if exit_code is not None and not _integer(exit_code):
                    errors.append(f"autonomy_state_attempt_exit_code_invalid:{label}")
            if index < len(attempts) - 1 and attempt_state not in TERMINAL_WORKFLOW_STATES:
                errors.append(f"autonomy_state_nonterminal_prior_attempt:{label}")
            if started is not None:
                prior_started = started
            if finished is not None:
                prior_finished = finished

        last = attempts[-1]
        if isinstance(last, dict):
            if workflow.get("latest_run_id") != last.get("run_id"):
                errors.append(f"autonomy_state_latest_run_id_mismatch:{name}")
            if workflow.get("state") != last.get("state"):
                errors.append(f"autonomy_state_latest_state_mismatch:{name}")

    events = document.get("events")
    if not isinstance(events, list):
        errors.append("autonomy_state_events_invalid")
        events = []
    prior_event_time: dt.datetime | None = None
    events_by_run: dict[str, list[dict]] = {}
    for index, event in enumerate(events):
        label = str(index)
        if not isinstance(event, dict) or set(event) != STATE_EVENT_KEYS:
            errors.append(f"autonomy_state_event_shape_invalid:{label}")
            continue
        if not _integer(event.get("seq"), minimum=0) or event.get("seq") != index:
            errors.append(f"autonomy_state_event_sequence_invalid:{label}")
        event_time = _state_timestamp(event.get("utc"))
        if event_time is None:
            errors.append(f"autonomy_state_event_utc_invalid:{label}")
        else:
            observed_times.append(event_time)
            if prior_event_time is not None and event_time < prior_event_time:
                errors.append(f"autonomy_state_event_time_order_invalid:{label}")
            prior_event_time = event_time
        event_workflow = event.get("workflow")
        if event_workflow not in WORKFLOWS:
            errors.append(f"autonomy_state_event_workflow_invalid:{label}")
        run_id = event.get("run_id")
        if not _valid_run_id(run_id):
            errors.append(f"autonomy_state_event_run_id_invalid:{label}")
        elif run_id not in seen_run_ids:
            errors.append(f"autonomy_state_event_run_id_unknown:{label}")
        else:
            events_by_run.setdefault(run_id, []).append(event)
            if run_workflows.get(run_id) != event_workflow:
                errors.append(f"autonomy_state_event_workflow_run_mismatch:{label}")
        for field in ("event", "reason"):
            value = event.get(field)
            if not isinstance(value, str) or SAFE_TOKEN_RE.fullmatch(value) is None:
                errors.append(f"autonomy_state_event_{field}_invalid:{label}")

    # Every attempt must be backed by its own lifecycle events. Merely listing a terminal row is
    # not proof that the supervisor actually started or finished that run.
    for name, index, attempt in attempt_records:
        label = f"{name}:{index}"
        run_id = attempt.get("run_id")
        rows = events_by_run.get(run_id, []) if isinstance(run_id, str) else []
        starts = [row for row in rows if row.get("event") == "attempt_started"]
        runners = [row for row in rows if row.get("event") == "runner_started"]
        finishes = [row for row in rows if row.get("event") == "attempt_finished"]
        stale = [row for row in rows if row.get("event") == "stale_owner_recovered"]
        expected_start_reason = "dry_run" if attempt.get("dry_run") is True else "production"
        if (len(starts) != 1 or starts[0].get("utc") != attempt.get("started_utc")
                or starts[0].get("reason") != expected_start_reason):
            errors.append(f"autonomy_state_attempt_started_event_invalid:{label}")
        child_present = attempt.get("child_pid") is not None
        if child_present:
            if (len(runners) != 1
                    or runners[0].get("reason") != "allowlisted_runner"):
                errors.append(f"autonomy_state_runner_started_event_invalid:{label}")
        elif runners:
            errors.append(f"autonomy_state_unexpected_runner_event:{label}")
        attempt_state = attempt.get("state")
        if attempt_state in TERMINAL_WORKFLOW_STATES:
            ordinary_finish = (
                len(finishes) == 1
                and finishes[0].get("utc") == attempt.get("finished_utc")
                and finishes[0].get("reason") == attempt.get("outcome"))
            stale_finish = (
                attempt.get("outcome") == "stale_owner_recovered"
                and len(stale) == 1
                and stale[0].get("utc") == attempt.get("finished_utc")
                and stale[0].get("reason") == "owner_pid_not_alive")
            post_close_seal_failure = (
                name == "post_close"
                and attempt.get("outcome") == "post_close_terminal_receipt_failed"
                and len(finishes) == 1
                and any(row.get("event") == "terminal_receipt_failed"
                        and row.get("utc") == attempt.get("finished_utc") for row in rows))
            if not (ordinary_finish or stale_finish or post_close_seal_failure):
                errors.append(f"autonomy_state_attempt_finished_event_invalid:{label}")
        elif finishes or stale:
            errors.append(f"autonomy_state_nonterminal_finish_event:{label}")

    errors.extend(_macro_history_errors(document, target))
    if updated is not None and any(value > updated for value in observed_times):
        errors.append("autonomy_state_updated_before_observed_activity")
    return (document if not errors else None), sorted(set(errors))


def _safe_failure(run_status: dict | None, target: str) -> bool:
    return bool(
        isinstance(run_status, dict)
        and run_status.get("target_session_date") == target
        and run_status.get("status") == "failed"
        and run_status.get("no_order_or_mutation_invoked") is True
        and run_status.get("execution_enabled") is False
        and run_status.get("counts_in_metrics") is False
        and run_status.get("ledger_incremented") is False
        and run_status.get("preliminary_checkpoint_written") is True
    )


def _macro_receipt_binding_errors(record: dict, target: str, root: Path) -> list[str]:
    """Bind a successful macro terminal to the one canonical validated receipt."""
    attempts = record.get("attempts")
    latest = attempts[-1] if isinstance(attempts, list) and attempts else None
    if not isinstance(latest, dict):
        return ["macro_refresh_latest_attempt_missing"]
    artifact = root / "data" / "runtime" / "macro_actual_refresh" / f"{target}.json"
    if artifact.is_symlink() or not artifact.is_file():
        return ["macro_refresh_receipt_missing_or_symlink"]
    receipt, errors = macro_actual_refresh.load_valid_receipt(artifact, target=target)
    if receipt is None or errors:
        return ["macro_refresh_receipt_invalid"] + [
            f"macro_refresh_receipt:{error}" for error in errors[:20]]
    receipt_status = receipt.get("status")
    state_value = record.get("state")
    if ((state_value == "completed" and receipt_status not in {"ready", "no_due_events"})
            or (state_value == "degraded" and receipt_status != "degraded")):
        return ["macro_refresh_receipt_state_mismatch"]
    relative = f"data/runtime/macro_actual_refresh/{target}.json"
    expected_reason = ("macro_refresh_degraded" if receipt_status == "degraded"
                       else f"macro_refresh_{receipt_status}")
    # The orchestrator's terminal-success branch (autonomy_orchestrator.run_workflow) records
    # outcome="contract_satisfied" for EVERY completed workflow; only degraded terminals carry
    # the postcondition reason as the attempt outcome. Bind to the vocabulary production
    # actually writes (regression 2026-07-17: every completed macro attempt was false-failed,
    # taking the whole composite health artifact down with it).
    expected_outcome = ("contract_satisfied" if state_value == "completed"
                        else expected_reason)
    postconditions = latest.get("postconditions")
    if latest.get("child_pid") is not None:
        expected_keys = {
            "task_status", "task_detail", "task_state_fresh", "activation_authority",
            "execution_enabled", "status", "reason", "artifact", "artifact_sha256",
        }
        expected_status = "warn" if receipt_status == "degraded" else "pass"
        expected_task = "warning" if receipt_status == "degraded" else "success"
        if (not isinstance(postconditions, dict) or set(postconditions) != expected_keys
                or postconditions.get("task_status") != expected_task
                or postconditions.get("task_state_fresh") is not True
                or postconditions.get("activation_authority") is not False
                or postconditions.get("execution_enabled") is not False
                or postconditions.get("status") != expected_status
                or postconditions.get("reason") != expected_reason
                or postconditions.get("artifact") != relative
                or postconditions.get("artifact_sha256") != receipt.get("artifact_sha256")
                or latest.get("outcome") != expected_outcome):
            return ["macro_refresh_postcondition_binding_invalid"]
        generated = _parse_timestamp(receipt.get("generated_utc"))
        started = _state_timestamp(latest.get("started_utc"))
        finished = _state_timestamp(latest.get("finished_utc"))
        if (generated is None or started is None or finished is None
                or generated < started or generated > finished):
            return ["macro_refresh_receipt_attempt_time_mismatch"]
    else:
        preserved = {
            "ready": "existing_macro_receipt_preserved",
            "no_due_events": "existing_macro_receipt_preserved",
            "degraded": "existing_degraded_macro_receipt_preserved",
        }
        observation = (latest.get("preflight") or {}).get("existing_macro_receipt")
        if (postconditions is not None or latest.get("outcome") != preserved.get(receipt_status)
                or not isinstance(observation, dict)
                or observation.get("status") != "valid"
                or observation.get("receipt_status") != receipt_status
                or observation.get("artifact") != relative
                or observation.get("artifact_file_sha256") != _sha_file(artifact)
                or observation.get("receipt_artifact_sha256") != receipt.get("artifact_sha256")):
            return ["macro_refresh_preserved_receipt_binding_invalid"]
    return []


def _workflow_plane(state: dict | None, target: str,
                    run_status: dict | None, root: Path = ROOT) -> tuple[dict, list[str]]:
    valid, errors = _valid_state(state, target)
    output: dict[str, dict] = {}
    for name in WORKFLOWS:
        record = (valid.get("workflows") or {}).get(name) if valid else None
        if not isinstance(record, dict):
            output[name] = {
                "status": "unknown",
                "state": None,
                "reason": "workflow_state_unavailable",
                "latest_run_id": None,
                "outcome": None,
                "exit_code": None,
                "started_utc": None,
                "finished_utc": None,
            }
            continue
        attempts = record["attempts"]
        latest = attempts[-1] if attempts else {}
        state_value = record.get("state")
        outcome = latest.get("outcome")
        if state_value == "completed":
            status, reason = "healthy", "workflow_completed"
        elif state_value == "degraded":
            status, reason = "degraded", str(outcome or "workflow_degraded")
        elif state_value == "skipped" and outcome in EXPECTED_SKIP_OUTCOMES:
            status, reason = "skipped_expected", str(outcome)
        elif state_value == "skipped":
            status, reason = "skipped", str(outcome or "workflow_skipped")
        elif state_value == "failed" and name == "morning" and _safe_failure(run_status, target):
            status, reason = "failed_safe", str(outcome or "safe_failure_artifact_present")
        elif state_value in {"failed", "blocked"}:
            status, reason = str(state_value), str(outcome or state_value)
        elif state_value in {"pending", "running"}:
            status, reason = str(state_value), f"workflow_{state_value}"
        else:
            status, reason = "unknown", "workflow_state_invalid"
        if (name == "post_close" and state_value == "skipped"
                and outcome == post_close_maintenance_receipt.ABSENT_REASON
                and target >= MAINTENANCE_RECEIPT_EFFECTIVE_DATE):
            maintenance_errors = post_close_maintenance_receipt.validate_receipt(
                target=target,
                outer_run_id=str(record.get("latest_run_id") or ""),
                root=root,
                expected_state="skipped",
                state_document=valid,
            )
            if maintenance_errors:
                status, reason = "at_issue", "post_close_maintenance_receipt_invalid"
            else:
                status, reason = (
                    "skipped_expected", "post_close_maintenance_receipt_validated")
        if name == "post_close" and state_value in {"completed", "degraded"}:
            terminal_errors = post_close_finality.validate_terminal(
                target=target, supervised_run_id=str(record.get("latest_run_id") or ""),
                root=root)
            if terminal_errors:
                status, reason = "at_issue", "post_close_terminal_receipt_invalid"
            else:
                reason = "post_close_terminal_receipt_validated"
        if name == "post_open" and state_value in {"completed", "degraded"}:
            terminal_errors = post_open_finality.validate_terminal(
                target=target, run_id=str(record.get("latest_run_id") or ""),
                root=root)
            if terminal_errors:
                status, reason = "at_issue", "post_open_terminal_receipt_invalid"
            else:
                reason = "post_open_terminal_receipt_validated"
        if name == "macro_refresh" and state_value in {"completed", "degraded"}:
            macro_errors = _macro_receipt_binding_errors(record, target, root)
            if macro_errors:
                status, reason = "at_issue", macro_errors[0]
        output[name] = {
            "status": status,
            "state": state_value,
            "reason": reason,
            "latest_run_id": record.get("latest_run_id"),
            "outcome": outcome,
            "exit_code": latest.get("exit_code"),
            "started_utc": latest.get("started_utc"),
            "finished_utc": latest.get("finished_utc"),
        }
    return output, errors


def _at_pt(target: str, value: str) -> dt.datetime:
    target_date = _target_date(target)
    parsed = _time_minutes(value)
    if parsed is None:
        raise ValueError("service_slo_time_invalid")
    return dt.datetime.combine(
        target_date, dt.time(parsed // 60, parsed % 60), PT)


def _market_day_for_target(target: str, override: dict | None = None) -> dict:
    if override is None:
        holidays, early = validate_market_day.load_calendar(
            validate_market_day.CONFIG)
        document = validate_market_day.verdict(
            _target_date(target), holidays, early)
    else:
        document = override
    if (not isinstance(document, dict)
            or document.get("date") != target
            or not isinstance(document.get("is_trading_day"), bool)
            or not isinstance(document.get("calendar_covers_year"), bool)
            or not isinstance(document.get("reason"), str)):
        return {
            "status": "invalid",
            "reason": "market_calendar_verdict_invalid",
            "is_trading_day": False,
            "calendar_covers_year": False,
        }
    if document["calendar_covers_year"] is not True:
        return {
            "status": "invalid",
            "reason": "market_calendar_year_uncovered",
            "is_trading_day": False,
            "calendar_covers_year": False,
        }
    return {
        "status": (
            "trading_day" if document["is_trading_day"]
            else "non_trading_day"),
        "reason": document["reason"],
        "is_trading_day": document["is_trading_day"],
        "calendar_covers_year": True,
    }


def _service_row_base(target: str, policy: dict) -> dict:
    scheduled = _at_pt(target, policy["scheduled_pt"])
    start_deadline = _at_pt(target, policy["start_deadline_pt"])
    terminal_deadline = _at_pt(target, policy["terminal_deadline_pt"])
    return {
        "status": "invalid",
        "objective": policy["objective"],
        "scheduled_pt": scheduled.isoformat(),
        "scheduled_utc": scheduled.astimezone(UTC).isoformat(),
        "start_deadline_pt": start_deadline.isoformat(),
        "start_deadline_utc": start_deadline.astimezone(UTC).isoformat(),
        "terminal_deadline_pt": terminal_deadline.isoformat(),
        "terminal_deadline_utc": terminal_deadline.astimezone(UTC).isoformat(),
        # Compatibility aliases for the original Morning-only SLO surface.
        "deadline_pt": terminal_deadline.isoformat(),
        "deadline_utc": terminal_deadline.astimezone(UTC).isoformat(),
        "start_status": "invalid",
        "terminal_status": "invalid",
        "started_utc": None,
        "terminal_utc": None,
        "terminal_source": None,
        "start_lateness_seconds": None,
        "terminal_lateness_seconds": None,
        "reason": "service_slo_invalid",
        "receipt_errors": [],
        "validation_errors": [],
    }


def _service_slo_row(*, name: str, target: str, workflow: dict,
                     policy: dict, receipt: dict | None, root: Path,
                     now: dt.datetime, market: dict) -> dict:
    row = _service_row_base(target, policy)
    if market["status"] == "invalid":
        row.update(
            status="invalid", start_status="invalid",
            terminal_status="invalid", reason=market["reason"],
            validation_errors=[market["reason"]])
        return row
    if market["status"] == "non_trading_day":
        row.update(
            status="not_applicable", start_status="not_applicable",
            terminal_status="not_applicable", reason="non_trading_day")
        return row

    current = now.astimezone(UTC)
    start_deadline = dt.datetime.fromisoformat(row["start_deadline_utc"])
    terminal_deadline = dt.datetime.fromisoformat(row["terminal_deadline_utc"])
    raw_started = workflow.get("started_utc")
    started = _parse_timestamp(raw_started)
    validation_errors: list[str] = []
    if raw_started is not None and started is None:
        validation_errors.append("workflow_started_utc_invalid")
    if started is not None and started > current + dt.timedelta(minutes=1):
        validation_errors.append("workflow_started_utc_in_future")
    terminal_workflow_statuses = {
        "healthy", "degraded", "skipped_expected", "skipped",
        "failed", "failed_safe", "blocked",
    }
    if started is None and workflow.get("status") in terminal_workflow_statuses:
        validation_errors.append("terminal_workflow_missing_start")
    if validation_errors:
        start_status = "invalid"
        start_lateness = None
    elif started is None:
        start_status = "pending" if current <= start_deadline else "missed"
        start_lateness = None
    else:
        start_lateness = max(
            0, int((started - start_deadline).total_seconds()))
        start_status = "met" if started <= start_deadline else "missed"

    receipt_errors: list[str] = []
    terminal: dt.datetime | None = None
    terminal_source: str | None = None
    terminal_reason = "deadline_not_reached"
    terminal_lateness: int | None = None
    terminal_receipt_present = receipt is not None
    if policy["terminal_source"] == "validated_morning_terminal":
        freeze_terminal, freeze_errors = _validate_freeze_receipt(
            receipt, target, root, now=current)
        if freeze_terminal is not None:
            terminal = freeze_terminal
            terminal_source = "validated_freeze_receipt"
            receipt_errors = freeze_errors
        elif receipt is not None:
            # A present but invalid freeze receipt is never bypassed by a Preliminary artifact.
            receipt_errors = freeze_errors
        else:
            # Legacy v1 receipts are historical audit artifacts only. They neither satisfy nor
            # block the active terminal selector; only a governance-v2 freeze or deterministic
            # no-live receipt can satisfy the Morning SLO.
            terminal, receipt_errors = _validate_deterministic_terminal(target, root)
            if terminal is not None:
                terminal_source = "validated_deterministic_terminal_receipt"
        if terminal is not None and terminal > current + dt.timedelta(minutes=1):
            validation_errors.append("morning_terminal_utc_in_future")
            terminal = None
        if terminal is not None and started is not None and terminal < started:
            validation_errors.append("morning_terminal_before_workflow_start")
            terminal = None
        if terminal is not None:
            terminal_lateness = max(
                0, int((terminal - terminal_deadline).total_seconds()))
            terminal_status = "met" if terminal <= terminal_deadline else "missed"
            if terminal_source == "validated_freeze_receipt":
                terminal_reason = (
                    "validated_freeze_by_deadline" if terminal_status == "met"
                    else "validated_freeze_after_deadline")
            else:
                terminal_reason = (
                    "validated_deterministic_terminal_by_deadline"
                    if terminal_status == "met"
                    else "validated_deterministic_terminal_after_deadline")
        elif terminal_receipt_present and receipt_errors:
            terminal_status = "missed"
            terminal_reason = "freeze_receipt_invalid"
        elif workflow.get("status") in terminal_workflow_statuses:
            terminal_status = "missed"
            terminal_reason = "workflow_failed_before_expected_terminal"
        elif current <= terminal_deadline:
            terminal_status = "pending"
        else:
            terminal_status = "missed"
            terminal_reason = "no_validated_terminal_by_deadline"
    else:
        raw_finished = workflow.get("finished_utc")
        finished = _parse_timestamp(raw_finished)
        if raw_finished is not None and finished is None:
            validation_errors.append("workflow_finished_utc_invalid")
        if finished is not None and finished > current + dt.timedelta(minutes=1):
            validation_errors.append("workflow_finished_utc_in_future")
        if finished is not None and started is not None and finished < started:
            validation_errors.append("workflow_finished_before_start")
        accepted = workflow.get("status") in set(
            policy["accepted_terminal_workflow_statuses"])
        if workflow.get("status") == "skipped_expected":
            accepted = (
                workflow.get("outcome") in policy["expected_skip_outcomes"])
            if not accepted:
                validation_errors.append("unexpected_skip_outcome")
        if accepted and finished is None:
            validation_errors.append("accepted_terminal_missing_finished_utc")
        if accepted and finished is not None:
            terminal = finished
            terminal_source = "validated_autonomy_attempt"
            terminal_lateness = max(
                0, int((terminal - terminal_deadline).total_seconds()))
            terminal_status = "met" if terminal <= terminal_deadline else "missed"
            terminal_reason = (
                "validated_attempt_by_deadline" if terminal_status == "met"
                else "validated_attempt_after_deadline")
        elif workflow.get("status") in terminal_workflow_statuses:
            terminal_status = "missed"
            terminal_reason = "workflow_failed_before_expected_terminal"
        elif current <= terminal_deadline:
            terminal_status = "pending"
        else:
            terminal_status = "missed"
            terminal_reason = "no_validated_terminal_by_deadline"

    if validation_errors:
        status, reason = "invalid", sorted(set(validation_errors))[0]
        if start_status != "invalid":
            start_status = "invalid"
        terminal_status = "invalid"
    elif start_status == "missed":
        status = "missed"
        reason = (
            "start_deadline_missed" if started is None
            else "started_after_deadline")
    elif terminal_status == "missed":
        status, reason = "missed", terminal_reason
    elif start_status == "pending" or terminal_status == "pending":
        status = "pending"
        reason = (
            "waiting_for_start" if start_status == "pending"
            else "waiting_for_terminal")
    else:
        status, reason = "met", "start_and_terminal_met"
    row.update({
        "status": status,
        "start_status": start_status,
        "terminal_status": terminal_status,
        "started_utc": started.isoformat() if started is not None else None,
        "terminal_utc": terminal.isoformat() if terminal is not None else None,
        "terminal_source": terminal_source,
        "start_lateness_seconds": start_lateness,
        "terminal_lateness_seconds": terminal_lateness,
        "reason": reason,
        "receipt_errors": receipt_errors,
        "validation_errors": sorted(set(validation_errors)),
    })
    return row


def _service_slo_planes(*, target: str, workflow_plane: dict,
                        receipt: dict | None, root: Path, now: dt.datetime,
                        policy: dict | None = None,
                        market_day: dict | None = None) -> tuple[dict, dict]:
    if policy is None:
        policy, policy_errors = _load_service_slo_policy()
    else:
        policy_errors = validate_service_slo_policy(policy)
    market = _market_day_for_target(target, market_day)
    policy_hash = (
        hashlib.sha256(_canonical(policy)).hexdigest()
        if isinstance(policy, dict) else None)
    errors = list(policy_errors)
    if market["status"] == "invalid":
        errors.append(market["reason"])
    metadata = {
        "status": "ready" if not errors else "invalid",
        "schema": (
            policy.get("schema") if isinstance(policy, dict) else None),
        "version": (
            policy.get("version") if isinstance(policy, dict) else None),
        "policy_sha256": policy_hash,
        "market_day_status": market["status"],
        "market_day_reason": market["reason"],
        "calendar_covers_year": market["calendar_covers_year"],
        "errors": sorted(set(errors)),
        **SERVICE_SLO_SAFETY,
    }
    if errors or not isinstance(policy, dict):
        fallback = EXPECTED_SERVICE_SLOS
        rows = {}
        for name in WORKFLOWS:
            rows[name] = _service_row_base(target, fallback[name])
            rows[name].update(
                status="invalid", start_status="invalid",
                terminal_status="invalid", reason="service_slo_policy_invalid",
                validation_errors=sorted(set(errors)))
        return metadata, rows
    rows = {
        name: _service_slo_row(
            name=name, target=target, workflow=workflow_plane[name],
            policy=policy["workflows"][name], receipt=receipt, root=root,
            now=now, market=market)
        for name in WORKFLOWS
    }
    return metadata, rows


def _broker_foundation(document: dict | None, target: str, root: Path) -> dict:
    if not isinstance(document, dict):
        return {
            "status": "unobservable", "zero_preview_write_verified": False,
            "reason": "broker_runtime_sli_missing_or_unreadable",
            "broker_read_attempt_total": 0, "broker_preview_write_attempt_total": 0,
            "unknown_broker_attempt_total": 0, "receipt_count": 0, "sli_sha256": None,
        }
    errors = broker_runtime_audit.validate_sli_against_sources(
        document,
        audit_root=root / "data" / "runtime" / "broker_audit",
        target=target,
    )
    if errors:
        return {
            "status": "invalid", "zero_preview_write_verified": False,
            "reason": "broker_runtime_sli_invalid", "errors": sorted(set(errors)),
            "broker_read_attempt_total": 0, "broker_preview_write_attempt_total": 0,
            "unknown_broker_attempt_total": 0, "receipt_count": 0,
            "sli_sha256": document.get("sli_sha256"),
        }
    status = document["status"]
    return {
        "status": status,
        "zero_preview_write_verified": document["zero_preview_write_verified"],
        "reason": "validated_runtime_attempt_receipts",
        "broker_read_attempt_total": document["broker_read_attempt_total"],
        "broker_preview_write_attempt_total": document["broker_preview_write_attempt_total"],
        "unknown_broker_attempt_total": document["unknown_broker_attempt_total"],
        "receipt_count": document["receipt_count"],
        "sli_sha256": document["sli_sha256"],
    }


def _current_repo_head(root: Path = ROOT) -> str | None:
    """Best-effort current HEAD for release-provenance freshness binding (read-only)."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(root).resolve()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    head = result.stdout.strip()
    return head if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else None


def _release_foundation(document: dict | None, *, root: Path = ROOT) -> dict:
    """Project forensic release truth and the narrower runtime-source authority separately."""
    unavailable = {
        "status": "unavailable",
        "release_status": "unavailable",
        "release_reproducible": False,
        "runtime_provenance_status": "unavailable",
        "runtime_provenance_ok": False,
        "runtime_binding_status": "unavailable",
        "runtime_authority_passed": False,
        "reason": "release_provenance_gate_missing_or_unreadable",
        "runtime_reason_codes": [],
        "snapshot_id": None,
        "gate_sha256": None,
        "drift": [],
        "head_commit": None,
        "validated_generated_output_count": 0,
    }
    if not isinstance(document, dict):
        return unavailable

    current_head = _current_repo_head(root)
    gate_head = document.get("head_commit")
    head_errors: list[str] = []
    if not isinstance(gate_head, str) or not re.fullmatch(r"[0-9a-f]{40}", gate_head):
        head_errors.append("release_provenance_head_commit_unusable")
    if not isinstance(current_head, str) or not re.fullmatch(r"[0-9a-f]{40}", current_head):
        head_errors.append("release_provenance_current_head_unresolvable")
    elif isinstance(gate_head, str) and gate_head != current_head:
        head_errors.append("release_provenance_head_mismatch")

    # Compatibility: stored callers historically supplied the strict self-hashed
    # release_provenance_gate/1.0 directly. Its exact, reproducible, current-HEAD match remains
    # usable as forensic evidence only; it can never imply the new runtime-source authority.
    legacy_shape = (
        document.get("schema") == release_snapshot.GATE_SCHEMA
        or (not any(key in document for key in {
            "runtime_binding_status", "release_snapshot_verification", "reason_codes"})
            and {"status", "release_reproducible", "snapshot_id", "gate_sha256",
                 "head_commit", "drift"}.issubset(document)))
    if legacy_shape:
        legacy_errors = release_snapshot.validate_gate(document)
        release_status = str(document.get("status") or "unavailable")
        if legacy_errors:
            return {
                **unavailable,
                "status": "invalid",
                "release_status": release_status,
                "reason": "release_provenance_gate_invalid",
                "snapshot_id": document.get("snapshot_id")
                if isinstance(document.get("snapshot_id"), str) else None,
                "gate_sha256": document.get("gate_sha256")
                if isinstance(document.get("gate_sha256"), str) else None,
                "head_commit": gate_head if isinstance(gate_head, str) else None,
                "errors": sorted(set(legacy_errors)),
            }
        if head_errors:
            if "release_provenance_head_mismatch" in head_errors:
                projected_status = "stale_reference"
                projected_reason = "release_provenance_head_mismatch"
            else:
                projected_status = "unverified_reference"
                projected_reason = head_errors[0]
            return {
                **unavailable,
                "status": projected_status,
                "release_status": release_status,
                "reason": projected_reason,
                "runtime_provenance_status": "blocked",
                "runtime_binding_status": "blocked",
                "runtime_reason_codes": sorted(set(head_errors)),
                "snapshot_id": document.get("snapshot_id")
                if isinstance(document.get("snapshot_id"), str) else None,
                "gate_sha256": document.get("gate_sha256")
                if isinstance(document.get("gate_sha256"), str) else None,
                "head_commit": gate_head if isinstance(gate_head, str) else None,
                "gate_head_commit": gate_head if isinstance(gate_head, str) else None,
                "current_head_commit": current_head,
                "drift": list(document.get("drift") or []),
            }
        forensic_match = bool(
            release_status == "match" and document.get("release_reproducible") is True)
        return {
            "status": release_status,
            "release_status": release_status,
            "release_reproducible": document.get("release_reproducible") is True,
            # Legacy gates establish forensic exactness only. They predate the runtime-source
            # and critical-byte checks and therefore cannot grant today's runtime authority.
            "runtime_provenance_status": "unavailable" if forensic_match else "blocked",
            "runtime_provenance_ok": False,
            "runtime_binding_status": "unavailable" if forensic_match else "blocked",
            "runtime_authority_passed": False,
            "reason": ("legacy_release_snapshot_forensic_match_only"
                       if forensic_match else "legacy_release_snapshot_not_current"),
            "runtime_reason_codes": (["runtime_source_binding_unavailable"]
                                     if forensic_match else
                                     ["release_snapshot_not_current"]),
            "snapshot_id": document["snapshot_id"],
            "gate_sha256": document["gate_sha256"],
            "drift": list(document.get("drift") or []),
            "head_commit": gate_head,
            "current_head_commit": current_head,
            "validated_generated_output_count": 0,
        }

    runtime_status = document.get("status")
    runtime_ok = document.get("ok")
    binding = document.get("runtime_binding_status")
    release_status = document.get("release_status")
    reasons = document.get("reason_codes")
    snapshot_id = document.get("release_snapshot_id")
    forensic = document.get("release_snapshot_verification")
    structural_errors: list[str] = []
    if document.get("schema") != runtime_provenance.SCHEMA:
        structural_errors.append("runtime_provenance_schema_invalid")
    structural_errors.extend(head_errors)
    if not isinstance(runtime_ok, bool) or runtime_status not in {"ready", "blocked"}:
        structural_errors.append("runtime_provenance_status_invalid")
    if binding not in {"match", "expected_output_drift", "blocked", "unavailable"}:
        structural_errors.append("runtime_binding_status_invalid")
    if release_status not in {"match", "drift", "invalid_snapshot", "unavailable"}:
        structural_errors.append("release_status_invalid")
    if not isinstance(reasons, list) or reasons != sorted(set(reasons)) \
            or any(not isinstance(value, str) or not SAFE_TOKEN_RE.fullmatch(value)
                   for value in reasons):
        structural_errors.append("runtime_reason_codes_invalid")
        reasons = []
    if snapshot_id is not None and (not isinstance(snapshot_id, str)
                                    or not SAFE_TOKEN_RE.fullmatch(snapshot_id)):
        structural_errors.append("release_snapshot_id_invalid")
    forensic_errors: list[str] = []
    if forensic is not None:
        forensic_errors = release_snapshot.validate_gate(forensic)
        if forensic_errors:
            structural_errors.append("release_snapshot_verification_invalid")
        elif (forensic.get("status") != release_status
              or forensic.get("snapshot_id") != snapshot_id
              or forensic.get("head_commit") != gate_head
              or forensic.get("drift") != document.get("release_drift")
              or forensic.get("errors") != document.get("release_errors")):
            structural_errors.append("release_snapshot_projection_mismatch")
    elif release_status in {"match", "drift", "invalid_snapshot"}:
        structural_errors.append("release_snapshot_verification_missing")

    release_reproducible = bool(
        isinstance(forensic, dict) and not forensic_errors
        and forensic.get("release_reproducible") is True)
    if binding == "match" and (release_status != "match" or not release_reproducible):
        structural_errors.append("runtime_match_forensic_mismatch")
    if binding == "expected_output_drift" \
            and (release_status != "drift" or release_reproducible):
        structural_errors.append("runtime_output_drift_forensic_mismatch")
    if runtime_ok is True and (runtime_status != "ready" or reasons
                               or binding not in RUNTIME_AUTHORITY_BINDINGS):
        structural_errors.append("runtime_provenance_ready_semantics_invalid")
    if runtime_ok is True:
        empty_runtime_lists = (
            "modified_runtime_paths", "staged_runtime_paths", "all_staged_paths",
            "untracked_runtime_paths", "critical_missing", "modified_critical_paths",
        )
        if any(document.get(key) != [] for key in empty_runtime_lists) \
                or document.get("critical_head_verification_available") is not True:
            structural_errors.append("runtime_provenance_source_safety_invalid")
        outputs = document.get("validated_generated_output_paths")
        output_reasons = document.get("output_drift_reasons")
        if binding == "match" and (
                document.get("release_drift") != [] or outputs != []
                or output_reasons != []):
            structural_errors.append("runtime_match_output_semantics_invalid")
        if binding == "expected_output_drift" and (
                not isinstance(outputs, list) or not outputs
                or not all(isinstance(value, str) and value for value in outputs)
                or output_reasons != []
                or not isinstance(document.get("release_drift"), list)
                or not document["release_drift"]
                or not set(document["release_drift"]).issubset({
                    "status_v2_z", "unstaged_patch", "untracked_inventory"})
                or not isinstance(forensic, dict)
                or any((forensic.get("comparisons") or {}).get(key) is not True
                       for key in ("head_commit", "head_ref", "staged_patch"))):
            structural_errors.append("runtime_output_drift_semantics_invalid")
    if runtime_ok is False and runtime_status != "blocked":
        structural_errors.append("runtime_provenance_blocked_semantics_invalid")

    if structural_errors:
        return {
            **unavailable,
            "status": "invalid",
            "release_status": release_status if isinstance(release_status, str)
            else "unavailable",
            "reason": "release_provenance_gate_invalid",
            "runtime_reason_codes": list(reasons),
            "snapshot_id": snapshot_id if isinstance(snapshot_id, str) else None,
            "errors": sorted(set(structural_errors + forensic_errors)),
        }

    runtime_authority = bool(runtime_ok is True and binding in RUNTIME_AUTHORITY_BINDINGS)
    raw_drift = document.get("release_drift")
    drift = list(raw_drift) if isinstance(raw_drift, list) else []
    outputs = document.get("validated_generated_output_paths")
    return {
        "status": str(release_status),
        "release_status": str(release_status),
        "release_reproducible": release_reproducible,
        "runtime_provenance_status": str(runtime_status),
        "runtime_provenance_ok": runtime_ok is True,
        "runtime_binding_status": str(binding),
        "runtime_authority_passed": runtime_authority,
        "reason": ("validated_runtime_source_binding" if runtime_authority
                   else "runtime_source_binding_blocked"),
        "runtime_reason_codes": list(reasons),
        "snapshot_id": snapshot_id,
        "gate_sha256": forensic.get("gate_sha256") if isinstance(forensic, dict) else None,
        "drift": drift,
        "head_commit": document.get("head_commit")
        if isinstance(document.get("head_commit"), str) else None,
        "validated_generated_output_count": len(outputs)
        if isinstance(outputs, list) else 0,
    }


def _free_source_foundation(document: dict | None) -> dict:
    """Expose validated aggregate collection health without importing provider payloads."""
    unavailable = {
        "status": "unavailable",
        "ok": False,
        "reason": "free_source_health_missing_or_unreadable",
        "artifact_sha256": None,
        "window": {
            "window_start": None,
            "window_end": None,
            "valid_session_count": 0,
            "invalid_session_count": 0,
        },
        "provider_health_counts": {},
        "public_problem_source_ids": [],
        "lane_statuses": {},
        "manual_actions": [],
        "recommendations": [],
        "maximum_external_spend_usd": "0.00",
        "source_authority_granted": False,
        "buy_authority_granted": False,
        "execution_enabled": False,
        "production_gate_enforced": False,
    }
    if not isinstance(document, dict):
        return unavailable
    errors = source_health.validate_receipt(document)
    if errors:
        return {
            **unavailable,
            "status": "invalid",
            "reason": "free_source_health_invalid",
            "artifact_sha256": document.get("artifact_sha256"),
            "errors": errors,
        }
    health_counts: dict[str, int] = {}
    public_problems: list[str] = []
    for source_id, provider in document["providers"].items():
        label = provider["health_label"]
        health_counts[label] = health_counts.get(label, 0) + 1
        if (provider["expected_class"] == "public_zero_key"
                and label in {"degraded", "partial"}):
            public_problems.append(source_id)
    window = document["window"]
    return {
        "status": document["status"],
        "ok": document["ok"],
        "reason": "validated_hash_linked_zero_dollar_collection_health",
        "artifact_sha256": document["artifact_sha256"],
        "window": {
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "valid_session_count": window["valid_session_count"],
            "invalid_session_count": window["invalid_session_count"],
        },
        "provider_health_counts": dict(sorted(health_counts.items())),
        "public_problem_source_ids": sorted(public_problems),
        "lane_statuses": {
            lane: value["status"] for lane, value in sorted(document["lanes"].items())
        },
        "manual_actions": document["manual_actions"],
        "recommendations": document["recommendations"],
        "maximum_external_spend_usd": document["maximum_external_spend_usd"],
        "source_authority_granted": False,
        "buy_authority_granted": False,
        "execution_enabled": False,
        "production_gate_enforced": False,
    }


def _free_source_alert_foundation(document: dict | None) -> dict:
    unavailable = {
        "status": "unavailable",
        "transition": "unavailable",
        "severity": "none",
        "active_codes": [],
        "notification_required": False,
        "notification_kind": "none",
        "state_written": False,
        "reason": "free_source_alert_missing_or_unreadable",
        "artifact_sha256": None,
        "notification_delivery_confirmed": False,
        "production_gate_enforced": False,
        "execution_enabled": False,
    }
    if not isinstance(document, dict):
        return unavailable
    errors = source_alert.validate_receipt(document)
    if errors:
        return {
            **unavailable,
            "status": "invalid",
            "transition": "state_invalid",
            "severity": "critical",
            "reason": "free_source_alert_invalid",
            "artifact_sha256": document.get("artifact_sha256"),
            "errors": errors,
        }
    return {
        "status": document["status"],
        "transition": document["transition"],
        "severity": document["severity"],
        "active_codes": document["active_codes"],
        "notification_required": document["notification_required"],
        "notification_kind": document["notification_kind"],
        "state_written": document["state_written"],
        "reason": "validated_local_alert_transition_decision",
        "artifact_sha256": document["artifact_sha256"],
        "notification_delivery_confirmed": False,
        "production_gate_enforced": False,
        "execution_enabled": False,
    }


def _power_plane(document: dict | None) -> dict:
    """Project a validated wake receipt without exposing raw pmset output."""
    unavailable = {
        "status": "unavailable",
        "ok": False,
        "reason": "power_schedule_health_missing_or_unreadable",
        "selected_event": None,
        "lead_minutes": None,
        "minutes_after_required": None,
        "reason_codes": [],
        "remediation_required": False,
        "manual_admin_required": False,
        "remediation_command": None,
        "artifact_sha256": None,
        "read_only": True,
        "diagnostic_only": True,
        "system_settings_changed": False,
        "execution_enabled": False,
        "source_authority_granted": False,
        "buy_authority_granted": False,
    }
    if not isinstance(document, dict):
        return unavailable
    errors = power_health.validate_receipt(document)
    if errors:
        return {
            **unavailable,
            "status": "invalid",
            "reason": "power_schedule_health_invalid",
            "artifact_sha256": document.get("artifact_sha256"),
            "errors": errors,
        }
    selected = document["selected_event"]
    selected_projection = None if selected is None else {
        "event_type": selected["event_type"],
        "time_pt": selected["time_pt"],
        "weekday_coverage": selected["weekday_coverage"],
    }
    return {
        "status": document["status"],
        "ok": document["ok"],
        "reason": "validated_read_only_pmset_schedule_inspection",
        "selected_event": selected_projection,
        "lead_minutes": document["lead_minutes"],
        "minutes_after_required": document["minutes_after_required"],
        "reason_codes": document["reason_codes"],
        "remediation_required": document["remediation_required"],
        "manual_admin_required": document["manual_admin_required"],
        "remediation_command": document["remediation_command"],
        "artifact_sha256": document["artifact_sha256"],
        "read_only": True,
        "diagnostic_only": True,
        "system_settings_changed": False,
        "execution_enabled": False,
        "source_authority_granted": False,
        "buy_authority_granted": False,
    }


def _research_artifact_plane(*, target: str, root: Path,
                             workflow_plane: dict,
                             candidate_health: dict | None,
                             candidate_quote_health: dict | None) -> dict:
    reasons: list[str] = []
    deterministic_terminal, _deterministic_errors = _validate_deterministic_terminal(
        target, root)
    expected_zero_candidate_terminal = deterministic_terminal is not None
    candidate = {
        "status": "unavailable",
        "health_label": None,
        "ok": False,
        "artifact_present": False,
        "artifact_valid": False,
        "source": "unavailable",
        "warnings": [],
    }
    if isinstance(candidate_health, dict):
        label = candidate_health.get("health_label")
        structurally_valid = (
            candidate_health.get("target_session_date") == target
            and label in {"healthy", "fallback", "empty", "missing", "invalid"}
            and candidate_health.get("diagnostic_only") is True
            and candidate_health.get("execution_enabled") is False
            and candidate_health.get("activation_authority") is False
            and isinstance(candidate_health.get("warnings"), list)
        )
        expected_ok = label in {"healthy", "fallback", "empty"}
        if not structurally_valid or candidate_health.get("ok") is not expected_ok:
            candidate["status"] = "invalid"
            reasons.append("candidate_health_receipt_invalid")
        else:
            candidate.update({
                "status": ("healthy" if label == "healthy" else
                           "degraded" if label in {"fallback", "empty"}
                           or (label == "missing" and expected_zero_candidate_terminal)
                           else "at_issue"),
                "health_label": label,
                "ok": candidate_health.get("ok") is True,
                "artifact_present": candidate_health.get("artifact_present") is True,
                "artifact_valid": candidate_health.get("artifact_valid") is True,
                "source": str(candidate_health.get("source") or "unavailable"),
                "warnings": [str(value)[:180]
                             for value in candidate_health.get("warnings", [])[:20]],
            })
            if label == "invalid":
                reasons.append("candidate_artifact_invalid")
            elif label == "missing" and not expected_zero_candidate_terminal:
                reasons.append("candidate_artifact_missing")

    quote = {
        "status": "unavailable",
        "ok": False,
        "artifact_status": None,
        "request_present": False,
        "bundle_present": False,
        "requested_count": 0,
        "fresh_count": 0,
        "actionable_count": 0,
        "unavailable_count": 0,
        "reason_codes": [],
        "request_sha256": None,
        "bundle_sha256": None,
        "buy_authority_granted": False,
        "execution_enabled": False,
    }
    if isinstance(candidate_quote_health, dict):
        quote_errors = candidate_quote_hydrator.validate_health(
            candidate_quote_health, target)
        if quote_errors:
            quote.update({
                "status": "invalid",
                "artifact_status": candidate_quote_health.get("status"),
                "reason_codes": quote_errors,
            })
            reasons.append("candidate_quote_health_receipt_invalid")
        else:
            artifact_status = candidate_quote_health["status"]
            projected_status = (
                "healthy" if artifact_status in {"ready", "empty"} else
                "degraded" if artifact_status in {"partial", "unavailable", "missing"}
                else "at_issue")
            quote.update({
                "status": projected_status,
                "ok": candidate_quote_health["ok"],
                "artifact_status": artifact_status,
                "request_present": candidate_quote_health["request_present"],
                "bundle_present": candidate_quote_health["bundle_present"],
                "requested_count": candidate_quote_health["requested_count"],
                "fresh_count": candidate_quote_health["fresh_count"],
                "actionable_count": candidate_quote_health["actionable_count"],
                "unavailable_count": candidate_quote_health["unavailable_count"],
                "reason_codes": [str(value)[:180] for value in
                                 candidate_quote_health["reason_codes"][:20]],
                "request_sha256": candidate_quote_health["request_sha256"],
                "bundle_sha256": candidate_quote_health["bundle_sha256"],
            })
            if artifact_status == "at_issue":
                reasons.append("candidate_quote_artifact_invalid")

    post_open = workflow_plane.get("post_open") or {}
    dependency = {
        "status": "not_applicable",
        "workflow_outcome": post_open.get("outcome"),
        "accepted_morning_artifact": None,
        "accepted_morning_kind": None,
        "resolution_reason": None,
    }
    if (post_open.get("status") == "skipped_expected"
            and post_open.get("outcome") == "no_official_morning_artifact"):
        accepted, kind, reason = orchestrator._official_morning_artifact(target, root)
        dependency.update({
            "status": "contradiction" if accepted is not None else "consistent_skip",
            "accepted_morning_artifact": (
                str(accepted.relative_to(root)) if accepted is not None else None),
            "accepted_morning_kind": kind if accepted is not None else None,
            "resolution_reason": reason,
        })
        if accepted is not None:
            reasons.append("post_open_skip_contradicts_official_morning")
        elif reason not in {"no_official_morning_artifact"}:
            dependency["status"] = "invalid_dependency_evidence"
            reasons.append(f"post_open_dependency_resolution_invalid:{reason}")

    status = "at_issue" if reasons else (
        "degraded" if "degraded" in {candidate["status"], quote["status"]} else
        "healthy" if "healthy" in {candidate["status"], quote["status"]} else
        "unavailable")
    return {
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "candidate_artifact": candidate,
        "candidate_quote_coverage": quote,
        "post_open_dependency": dependency,
        **RESEARCH_ARTIFACT_SAFETY,
    }


def compose(*, target: str, root: Path = ROOT, scheduler: dict | None,
            state: dict | None, run_status: dict | None,
            freeze_receipt: dict | None,
            now: dt.datetime | None = None,
            broker_sli: dict | None = None,
            release_gate: dict | None = None,
            free_source_receipt: dict | None = None,
            free_source_alert_receipt: dict | None = None,
            power_schedule: dict | None = None,
            candidate_artifact_health: dict | None = None,
            candidate_quote_health: dict | None = None,
            service_slo_policy: dict | None = None,
            market_day: dict | None = None) -> dict:
    _target_date(target)
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    scheduler_plane = _scheduler_plane(scheduler)
    workflow_plane, state_errors = _workflow_plane(state, target, run_status, root)
    service_policy_plane, service_plane = _service_slo_planes(
        target=target, workflow_plane=workflow_plane, receipt=freeze_receipt,
        root=root, now=current, policy=service_slo_policy,
        market_day=market_day)
    broker_foundation = _broker_foundation(broker_sli, target, root)
    # Library fixtures historically use non-Git temp roots with an explicitly injected gate.
    # A real alternate checkout has .git and must bind to its own HEAD, never this module's.
    release_root = root if (root / ".git").exists() else ROOT
    release_foundation = _release_foundation(release_gate, root=release_root)
    free_source_foundation = _free_source_foundation(free_source_receipt)
    free_source_alert_foundation = _free_source_alert_foundation(
        free_source_alert_receipt)
    power_plane = _power_plane(power_schedule)
    research_artifact_plane = _research_artifact_plane(
        target=target, root=root, workflow_plane=workflow_plane,
        candidate_health=candidate_artifact_health,
        candidate_quote_health=candidate_quote_health)
    release_evaluated = release_foundation["runtime_provenance_status"] in {
        "ready", "blocked"}
    if broker_foundation["status"] in {"violation", "invalid"} \
            or release_foundation["status"] == "invalid" \
            or (release_evaluated
                and release_foundation["runtime_authority_passed"] is not True):
        foundation_status = "at_issue"
    elif broker_foundation["status"] == "zero_verified" \
            and release_foundation["runtime_authority_passed"] is True:
        foundation_status = "healthy"
    else:
        foundation_status = "unverified"
    workflow_issue = any(item.get("status") in {
        "failed", "failed_safe", "blocked", "unknown", "at_issue"
    } for item in workflow_plane.values())
    power_issue = power_schedule is not None and power_plane["status"] != "ready"
    service_issue = any(
        service_plane[name]["status"] in {"missed", "invalid"}
        for name in WORKFLOWS)
    if (not scheduler_plane["ok"] or workflow_issue
            or service_issue or power_issue
            or research_artifact_plane["status"] == "at_issue"):
        overall = "at_issue"
    elif any(item.get("status") in {"pending", "running"}
             for item in workflow_plane.values()):
        overall = "in_progress"
    elif any(item.get("status") in {"degraded", "skipped"}
             for item in workflow_plane.values()):
        overall = "degraded"
    else:
        overall = "healthy"
    document = {
        "schema": SCHEMA,
        "target_session_date": target,
        "generated_utc": current.isoformat(),
        "overall_status": overall,
        "ok": overall != "at_issue",
        "scheduler_config_health": scheduler_plane,
        "workflow_health": workflow_plane,
        "service_slo_policy": service_policy_plane,
        "service_slo_health": service_plane,
        "power_schedule_health": power_plane,
        "research_artifact_health": research_artifact_plane,
        "foundation_safety": {
            "status": foundation_status,
            "production_gate_enforced": False,
            "broker_runtime": broker_foundation,
            "release_provenance": release_foundation,
            "free_source_health": free_source_foundation,
            "free_source_alert": free_source_alert_foundation,
        },
        "input_errors": state_errors,
        **SAFETY,
    }
    document["artifact_sha256"] = hashlib.sha256(_canonical(document)).hexdigest()
    return document


# Canonical macro_refresh binding-failure tokens (the exact first-error strings
# _macro_receipt_binding_errors can emit). A macro at_issue row carrying one of these must
# DEGRADE the document like post_open/post_close terminal-receipt issues do — not fail
# self-validation (regression 2026-07-17: any macro at_issue nuked the entire workflow_health
# surface via workflow_health_at_issue_scope_invalid and the watchdog exited rc=2 all day).
MACRO_AT_ISSUE_REASONS = frozenset({
    "macro_refresh_latest_attempt_missing",
    "macro_refresh_receipt_missing_or_symlink",
    "macro_refresh_receipt_invalid",
    "macro_refresh_receipt_state_mismatch",
    "macro_refresh_postcondition_binding_invalid",
    "macro_refresh_receipt_attempt_time_mismatch",
    "macro_refresh_preserved_receipt_binding_invalid",
})


def _validate_workflow_health_surface(document: dict) -> list[str]:
    """Validate the published projection independently of its artifact hash."""
    errors: list[str] = []
    workflows = document.get("workflow_health")
    input_errors = document.get("input_errors")
    input_errors_valid = (
        isinstance(input_errors, list)
        and all(isinstance(value, str) and value for value in input_errors)
        and input_errors == sorted(set(input_errors))
    )
    if not input_errors_valid:
        errors.append("input_errors_invalid")
    has_input_errors = bool(input_errors) if input_errors_valid else True
    if not isinstance(workflows, dict) or set(workflows) != set(WORKFLOWS):
        return errors + ["workflow_health_invalid"]

    seen_run_ids: set[str] = set()
    issue_statuses = {"unknown", "failed", "failed_safe", "blocked", "at_issue"}
    observed_issue = False
    expected_statuses = {
        "pending": {"pending"},
        "running": {"running"},
        "completed": {"healthy", "at_issue"},
        "degraded": {"degraded", "at_issue"},
        "skipped": {"skipped", "skipped_expected", "at_issue"},
        "failed": {"failed", "failed_safe"},
        "blocked": {"blocked"},
    }
    for name in WORKFLOWS:
        row = workflows.get(name)
        if not isinstance(row, dict) or set(row) != WORKFLOW_HEALTH_KEYS:
            errors.append(f"workflow_health_shape_invalid:{name}")
            continue
        status = row.get("status")
        state_value = row.get("state")
        if status not in WORKFLOW_HEALTH_STATUSES:
            errors.append(f"workflow_health_status_invalid:{name}")
        if status in issue_statuses:
            observed_issue = True
        reason = row.get("reason")
        if not isinstance(reason, str) or SAFE_TOKEN_RE.fullmatch(reason) is None:
            errors.append(f"workflow_health_reason_invalid:{name}")

        if status == "unknown":
            if not has_input_errors:
                errors.append(f"workflow_health_unknown_without_input_error:{name}")
            if any(row.get(key) is not None for key in (
                    "state", "latest_run_id", "outcome", "exit_code",
                    "started_utc", "finished_utc")):
                errors.append(f"workflow_health_unknown_payload_invalid:{name}")
            continue

        if state_value not in WORKFLOW_STATES:
            errors.append(f"workflow_health_state_invalid:{name}")
            continue
        if status not in expected_statuses[state_value]:
            errors.append(f"workflow_health_state_status_mismatch:{name}")
        if status == "failed_safe" and name != "morning":
            errors.append(f"workflow_health_failed_safe_scope_invalid:{name}")
        if status == "at_issue":
            allowed_terminal_issue = (
                (state_value in {"completed", "degraded"}
                 and ((name == "post_close"
                       and row.get("reason") == "post_close_terminal_receipt_invalid")
                      or (name == "post_open"
                          and row.get("reason") ==
                          "post_open_terminal_receipt_invalid")
                      or (name == "macro_refresh"
                          and row.get("reason") in MACRO_AT_ISSUE_REASONS)))
                or (name == "post_close" and state_value == "skipped"
                    and row.get("reason") ==
                    "post_close_maintenance_receipt_invalid"))
            if not allowed_terminal_issue:
                errors.append(f"workflow_health_at_issue_scope_invalid:{name}")
        if status == "skipped_expected" \
                and row.get("outcome") not in EXPECTED_SKIP_OUTCOMES:
            errors.append(f"workflow_health_expected_skip_outcome_invalid:{name}")

        run_id = row.get("latest_run_id")
        if state_value == "pending":
            if any(row.get(key) is not None for key in (
                    "latest_run_id", "outcome", "exit_code",
                    "started_utc", "finished_utc")):
                errors.append(f"workflow_health_pending_payload_invalid:{name}")
            continue
        if not _valid_run_id(run_id):
            errors.append(f"workflow_health_run_id_invalid:{name}")
        elif run_id in seen_run_ids:
            errors.append(f"workflow_health_duplicate_run_id:{run_id}")
        else:
            seen_run_ids.add(run_id)

        started = _state_timestamp(row.get("started_utc"))
        finished = _state_timestamp(row.get("finished_utc"))
        if started is None:
            errors.append(f"workflow_health_started_utc_invalid:{name}")
        if state_value == "running":
            if (row.get("outcome") is not None or row.get("exit_code") is not None
                    or row.get("finished_utc") is not None):
                errors.append(f"workflow_health_running_terminal_fields:{name}")
        else:
            outcome = row.get("outcome")
            if not isinstance(outcome, str) or SAFE_TOKEN_RE.fullmatch(outcome) is None:
                errors.append(f"workflow_health_outcome_invalid:{name}")
            if finished is None:
                errors.append(f"workflow_health_finished_utc_invalid:{name}")
            elif started is not None and finished < started:
                errors.append(f"workflow_health_finished_before_start:{name}")
            exit_code = row.get("exit_code")
            if exit_code is not None and not _integer(exit_code):
                errors.append(f"workflow_health_exit_code_invalid:{name}")

    if has_input_errors and not observed_issue:
        errors.append("workflow_health_input_error_hidden")
    if observed_issue:
        if document.get("overall_status") != "at_issue":
            errors.append("workflow_health_issue_hidden_by_overall_status")
        if document.get("ok") is not False:
            errors.append("workflow_health_issue_hidden_by_ok")
    return errors


def validate(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["artifact_not_object"]
    errors: list[str] = []
    if document.get("schema") != SCHEMA:
        errors.append("artifact_schema_invalid")
    try:
        _target_date(document.get("target_session_date"))
    except (TypeError, ValueError):
        errors.append("artifact_target_invalid")
    generated = _parse_timestamp(document.get("generated_utc"))
    if generated is None:
        errors.append("artifact_generated_utc_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) is not expected:
            errors.append(f"artifact_safety_field_invalid:{key}")
    if document.get("overall_status") not in {"healthy", "degraded", "in_progress", "at_issue"}:
        errors.append("overall_status_invalid")
    if not isinstance(document.get("ok"), bool) \
            or document.get("ok") is not (document.get("overall_status") != "at_issue"):
        errors.append("overall_ok_semantics_invalid")
    errors.extend(_validate_workflow_health_surface(document))
    foundation = document.get("foundation_safety")
    if not isinstance(foundation, dict):
        errors.append("foundation_safety_missing")
    else:
        if foundation.get("status") not in {"healthy", "unverified", "at_issue"}:
            errors.append("foundation_safety_status_invalid")
        if foundation.get("production_gate_enforced") is not False:
            errors.append("foundation_safety_authority_invalid")
        if (not isinstance(foundation.get("broker_runtime"), dict)
                or not isinstance(foundation.get("release_provenance"), dict)
                or not isinstance(foundation.get("free_source_health"), dict)
                or not isinstance(foundation.get("free_source_alert"), dict)):
            errors.append("foundation_safety_planes_invalid")
        else:
            release = foundation["release_provenance"]
            broker = foundation["broker_runtime"]
            runtime_fields = {
                "release_status", "runtime_provenance_status", "runtime_provenance_ok",
                "runtime_binding_status", "runtime_authority_passed",
            }
            historical_projection = not any(key in release for key in runtime_fields)
            if historical_projection:
                legacy_status = release.get("status")
                legacy_reproducible = release.get("release_reproducible")
                legacy_match = bool(
                    legacy_status == "match" and legacy_reproducible is True)
                if legacy_status not in {
                        "match", "drift", "invalid", "invalid_snapshot", "unavailable",
                        "unverified_reference", "stale_reference"} \
                        or not isinstance(legacy_reproducible, bool) \
                        or (legacy_reproducible is True) is not (legacy_status == "match"):
                    errors.append("foundation_legacy_release_projection_invalid")
                expected_foundation = (
                    "at_issue" if broker.get("status") in {"violation", "invalid"}
                    or legacy_status in {"drift", "invalid", "invalid_snapshot"}
                    else "healthy" if broker.get("status") == "zero_verified"
                    and legacy_match else "unverified")
            else:
                binding = release.get("runtime_binding_status")
                runtime_ok = release.get("runtime_provenance_ok")
                authority = release.get("runtime_authority_passed")
                expected_authority = bool(
                    runtime_ok is True and binding in RUNTIME_AUTHORITY_BINDINGS)
                if not isinstance(runtime_ok, bool) or not isinstance(authority, bool) \
                        or authority is not expected_authority:
                    errors.append("foundation_runtime_authority_semantics_invalid")
                if release.get("release_status") not in {
                        "match", "drift", "invalid_snapshot", "unavailable"} \
                        or not isinstance(release.get("release_reproducible"), bool):
                    errors.append("foundation_release_forensic_fields_invalid")
                if binding == "match" and (
                        release.get("release_status") != "match"
                        or release.get("release_reproducible") is not True):
                    errors.append("foundation_release_match_semantics_invalid")
                if binding == "expected_output_drift" and (
                        release.get("release_status") != "drift"
                        or release.get("release_reproducible") is not False):
                    errors.append("foundation_release_output_drift_semantics_invalid")
                release_evaluated = release.get("runtime_provenance_status") in {
                    "ready", "blocked"}
                expected_foundation = (
                    "at_issue" if broker.get("status") in {"violation", "invalid"}
                    or release.get("status") == "invalid"
                    or (release_evaluated and authority is not True)
                    else "healthy" if broker.get("status") == "zero_verified"
                    and authority is True else "unverified")
            if foundation.get("status") != expected_foundation:
                errors.append("foundation_safety_status_semantics_invalid")
    power_plane = document.get("power_schedule_health")
    if not isinstance(power_plane, dict):
        errors.append("power_schedule_health_missing")
    else:
        if power_plane.get("status") not in {
                "ready", "at_issue", "unavailable", "invalid"}:
            errors.append("power_schedule_health_status_invalid")
        for key, expected in {
                "read_only": True, "diagnostic_only": True,
                "system_settings_changed": False, "execution_enabled": False,
                "source_authority_granted": False,
                "buy_authority_granted": False}.items():
            if power_plane.get(key) is not expected:
                errors.append(f"power_schedule_health_safety_invalid:{key}")
    research_plane = document.get("research_artifact_health")
    if not isinstance(research_plane, dict):
        errors.append("research_artifact_health_missing")
    else:
        if research_plane.get("status") not in {
                "healthy", "degraded", "unavailable", "at_issue"}:
            errors.append("research_artifact_health_status_invalid")
        if not isinstance(research_plane.get("reason_codes"), list):
            errors.append("research_artifact_health_reasons_invalid")
        if not isinstance(research_plane.get("candidate_artifact"), dict):
            errors.append("research_artifact_candidate_surface_invalid")
        quote_surface = research_plane.get("candidate_quote_coverage")
        if not isinstance(quote_surface, dict):
            errors.append("research_artifact_candidate_quote_surface_invalid")
        else:
            if quote_surface.get("status") not in {
                    "healthy", "degraded", "unavailable", "at_issue", "invalid"}:
                errors.append("research_artifact_candidate_quote_status_invalid")
            if quote_surface.get("buy_authority_granted") is not False \
                    or quote_surface.get("execution_enabled") is not False:
                errors.append("research_artifact_candidate_quote_authority_invalid")
        if not isinstance(research_plane.get("post_open_dependency"), dict):
            errors.append("research_artifact_dependency_surface_invalid")
        for key, expected in RESEARCH_ARTIFACT_SAFETY.items():
            if research_plane.get(key) is not expected:
                errors.append(f"research_artifact_health_safety_invalid:{key}")
    service_policy = document.get("service_slo_policy")
    if not isinstance(service_policy, dict):
        errors.append("service_slo_policy_surface_missing")
    else:
        if service_policy.get("status") not in {"ready", "invalid"}:
            errors.append("service_slo_policy_surface_status_invalid")
        for key, expected in SERVICE_SLO_SAFETY.items():
            if service_policy.get(key) is not expected:
                errors.append(f"service_slo_policy_surface_safety_invalid:{key}")
    service_plane = document.get("service_slo_health")
    if not isinstance(service_plane, dict) or set(service_plane) != set(WORKFLOWS):
        errors.append("service_slo_health_invalid")
    else:
        for name in WORKFLOWS:
            row = service_plane.get(name)
            if not isinstance(row, dict):
                errors.append(f"service_slo_row_missing:{name}")
                continue
            if row.get("status") not in {
                    "met", "pending", "missed", "not_applicable", "invalid"}:
                errors.append(f"service_slo_row_status_invalid:{name}")
            if row.get("start_status") not in {
                    "met", "pending", "missed", "not_applicable", "invalid"}:
                errors.append(f"service_slo_start_status_invalid:{name}")
            if row.get("terminal_status") not in {
                    "met", "pending", "missed", "not_applicable", "invalid"}:
                errors.append(f"service_slo_terminal_status_invalid:{name}")
            if not isinstance(row.get("validation_errors"), list):
                errors.append(f"service_slo_validation_errors_invalid:{name}")
    expected_hash = hashlib.sha256(_canonical(_payload(document))).hexdigest()
    if document.get("artifact_sha256") != expected_hash:
        errors.append("artifact_sha256_mismatch")
    return errors


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile scheduler, workflow, and service-SLO health.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--scheduler-health")
    parser.add_argument("--autonomy-state")
    parser.add_argument("--run-status")
    parser.add_argument("--freeze-receipt")
    parser.add_argument("--broker-runtime-sli")
    parser.add_argument("--release-provenance-gate")
    parser.add_argument("--free-source-health")
    parser.add_argument("--free-source-alert")
    parser.add_argument("--power-schedule-health")
    parser.add_argument("--candidate-artifact-health")
    parser.add_argument("--candidate-quote-health")
    parser.add_argument("--service-slo-policy")
    parser.add_argument("--now", help="UTC/offset timestamp override for deterministic tests.")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        scheduler = (_read_json(Path(args.scheduler_health)) if args.scheduler_health else
                     scheduler_health.check_all(scheduler_health.DEFAULT_REPO_DIR,
                                                scheduler_health.DEFAULT_AGENTS_DIR))
        state = _read_json(Path(args.autonomy_state)) if args.autonomy_state else _read_json(
            root / "data" / "runtime" / "autonomy" / f"{args.target}.json")
        run_status = _read_json(Path(args.run_status)) if args.run_status else _read_json(
            root / "data" / "runtime" / "health" / f"{args.target}_run_status.json")
        receipt = _read_json(Path(args.freeze_receipt)) if args.freeze_receipt else _read_json(
            root / "data" / "runtime" / "research" / args.target / "freeze_receipt.json")
        broker_sli = (_read_json(Path(args.broker_runtime_sli))
                      if args.broker_runtime_sli else _read_json(
                          root / "data" / "runtime" / "broker_audit" /
                          args.target / "sli.json"))
        # Production health always evaluates the live checkout. The file override exists only
        # for bounded/offline fixtures and is still validated fail-closed by the projection.
        if args.release_provenance_gate:
            if not runtime_provenance.is_approved_test_root(root):
                raise ValueError("release_provenance_gate_override_test_only")
            release_gate = _read_json(Path(args.release_provenance_gate))
        else:
            release_gate = runtime_provenance.evaluate(root=root)
        free_source_receipt = (_read_json(Path(args.free_source_health))
                               if args.free_source_health else _read_json(
                                   root / "data" / "runtime" /
                                   "free_source_health" / "current.json"))
        free_source_alert_receipt = (_read_json(Path(args.free_source_alert))
                                     if args.free_source_alert else _read_json(
                                         root / "data" / "runtime" /
                                         "free_source_health" / "current_alert.json"))
        if args.power_schedule_health:
            power_schedule = _read_json(Path(args.power_schedule_health))
            if power_schedule is None:
                power_schedule = {
                    "schema": power_health.SCHEMA,
                    "error": "power_schedule_health_missing_or_unreadable",
                }
        else:
            power_policy, power_policy_errors = power_health.load_policy()
            if power_policy is None or power_policy_errors:
                power_schedule = {
                    "schema": power_health.SCHEMA,
                    "error": "power_schedule_policy_invalid",
                    "policy_errors": power_policy_errors,
                }
            else:
                power_schedule = power_health.inspect_live(policy=power_policy)
        service_slo_policy = (
            _read_json(Path(args.service_slo_policy))
            if args.service_slo_policy else None)
        if args.service_slo_policy and service_slo_policy is None:
            service_slo_policy = {
                "schema": SERVICE_SLO_POLICY_SCHEMA,
                "error": "service_slo_policy_missing_or_unreadable",
            }
        candidate_artifact_health = (
            _read_json(Path(args.candidate_artifact_health))
            if args.candidate_artifact_health else _read_json(
                root / "data" / "runtime" / "health" /
                f"{args.target}_candidate_artifact_health.json"))
        candidate_quote_health = (
            _read_json(Path(args.candidate_quote_health))
            if args.candidate_quote_health else _read_json(
                root / "data" / "runtime" / "health" /
                f"{args.target}_candidate_quote_health.json"))
        now = _parse_timestamp(args.now) if args.now else None
        if args.now and now is None:
            raise ValueError("now_invalid")
        document = compose(target=args.target, root=root, scheduler=scheduler, state=state,
                           run_status=run_status, freeze_receipt=receipt, now=now,
                           broker_sli=broker_sli, release_gate=release_gate,
                           free_source_receipt=free_source_receipt,
                           free_source_alert_receipt=free_source_alert_receipt,
                           power_schedule=power_schedule,
                           candidate_artifact_health=candidate_artifact_health,
                           candidate_quote_health=candidate_quote_health,
                           service_slo_policy=service_slo_policy)
        errors = validate(document)
        if errors:
            raise ValueError("artifact_self_validation_failed:" + ",".join(errors))
        destination = Path(args.out) if args.out else (
            root / "data" / "runtime" / "health" /
            f"{args.target}_autonomy_service_health.json")
        _atomic_write(destination, document)
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0 if document["ok"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), **SAFETY},
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
