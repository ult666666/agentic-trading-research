#!/usr/bin/env python3
"""Deduplicated local alert decisions for autonomy service-SLO health.

The evaluator validates a composite health receipt and writes only a small local transition state
and receipt. It never sends notifications; the watchdog shell wrapper translates exit 10/11 into
fixed local macOS text. No health prose, market data, account data, credentials, model, broker, or
trading authority enters this layer.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

import autonomy_service_health
import slo_persistence
from provider_adapters import base

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
CONFIG_PATH = ROOT / "config" / "autonomy_service_alerts.json"
POLICY_SCHEMA = "autonomy_service_alert_policy/1.0"
STATE_SCHEMA = "autonomy_service_alert_state/1.0"
RECEIPT_SCHEMA = "autonomy_service_alert_receipt/1.0"
WORKFLOWS = ("intake", "macro_refresh", "morning", "post_open", "post_close")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z0-9_.:-]+$")
POLICY_KEYS = {
    "schema", "version", "checkpoints_pt", "state_path", "receipt_path",
    "alert_on_missed_slo", "alert_on_invalid_slo",
    "alert_on_scheduler_config_issue", "notify_on_recovery",
    "repeat_unchanged_alerts", "safety",
}
SAFETY = {
    "diagnostic_only": True,
    "health_data_read_only": True,
    "local_state_write_only": True,
    "external_notification_enabled": False,
    "retry_authority": False,
    "schedule_change_allowed": False,
    "report_change_allowed": False,
    "strategy_change_allowed": False,
    "execution_enabled": False,
    "model_invoked": False,
    "broker_invoked": False,
    "contains_account_data": False,
    "contains_credentials": False,
}
EXPECTED_CHECKPOINTS = [
    {"workflow": "intake", "time_pt": "05:21"},
    {"workflow": "macro_refresh", "time_pt": "05:46"},
    {"workflow": "morning", "time_pt": "06:16"},
    {"workflow": "post_open", "time_pt": "07:06"},
    {"workflow": "post_close", "time_pt": "14:16"},
]
STATE_KEYS = {
    "schema", "updated_utc", "active_target_session_date",
    "active_fingerprint", "active_codes", "active_severity",
    "last_transition", "last_health_sha256", "notification_count",
    "last_notification_utc", "state_sha256", *SAFETY,
}
RECEIPT_KEYS = {
    "schema", "generated_utc", "target_session_date", "status",
    "transition", "severity", "health_overall_status",
    "health_artifact_sha256", "previous_active_fingerprint",
    "active_fingerprint", "active_codes", "notification_required",
    "notification_kind", "state_written", "errors", "artifact_sha256",
    "slo_persistence",
    *SAFETY,
}
TRANSITIONS = {
    "initialized", "new_alert", "changed_alert", "unchanged", "resolved", "clear",
}
PROHIBITED_KEYS = {
    "password", "token", "cookie", "authorization", "api_key",
    "account_number", "refresh_token", "secret", "email", "user_agent",
    "price", "position", "balance", "buying_power", "order",
}


def _relative_path(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value)
        and not Path(value).is_absolute() and ".." not in Path(value).parts)


def _minutes(value: Any) -> int | None:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["policy_not_object"]
    errors: list[str] = []
    if set(document) != POLICY_KEYS:
        errors.append("policy_keys_invalid")
    if document.get("schema") != POLICY_SCHEMA or document.get("version") != "1.0.0":
        errors.append("policy_version_invalid")
    if document.get("checkpoints_pt") != EXPECTED_CHECKPOINTS:
        errors.append("policy_checkpoints_invalid")
    for key in ("state_path", "receipt_path"):
        if not _relative_path(document.get(key)):
            errors.append(f"policy_path_invalid:{key}")
    for key in (
            "alert_on_missed_slo", "alert_on_invalid_slo",
            "alert_on_scheduler_config_issue", "notify_on_recovery",
            "repeat_unchanged_alerts"):
        if not isinstance(document.get(key), bool):
            errors.append(f"policy_boolean_invalid:{key}")
    for key in (
            "alert_on_missed_slo", "alert_on_invalid_slo",
            "alert_on_scheduler_config_issue", "notify_on_recovery"):
        if document.get(key) is not True:
            errors.append(f"policy_required_behavior_disabled:{key}")
    if document.get("repeat_unchanged_alerts") is not False:
        errors.append("policy_repeat_alerts_must_be_false")
    if document.get("safety") != SAFETY:
        errors.append("policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = CONFIG_PATH) -> tuple[dict | None, list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["policy_unreadable"]
    return document, validate_policy(document)


def cross_validate(path: Path = CONFIG_PATH) -> dict:
    policy, errors = load_policy(path)
    service_policy, service_errors = (
        autonomy_service_health._load_service_slo_policy())
    errors.extend(f"service_policy:{error}" for error in service_errors)
    if policy is not None and service_policy is not None:
        terminal_by_workflow = {
            name: service_policy["workflows"][name]["terminal_deadline_pt"]
            for name in WORKFLOWS
        }
        for checkpoint in policy["checkpoints_pt"]:
            terminal = _minutes(terminal_by_workflow[checkpoint["workflow"]])
            checkpoint_minute = _minutes(checkpoint["time_pt"])
            if (terminal is None or checkpoint_minute is None
                    or checkpoint_minute - terminal != 1):
                errors.append(
                    f"checkpoint_not_one_minute_after_terminal:"
                    f"{checkpoint['workflow']}")
    return {
        "ok": policy is not None and not errors,
        "errors": sorted(set(errors)),
        **SAFETY,
    }


def _has_prohibited_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in PROHIBITED_KEYS or _has_prohibited_key(child):
                return True
    elif isinstance(value, list):
        return any(_has_prohibited_key(child) for child in value)
    return False


def _safe_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and SHA_RE.fullmatch(value) else None


def _codes_valid(value: Any) -> bool:
    return (
        isinstance(value, list) and value == sorted(set(value))
        and all(isinstance(item, str) and CODE_RE.fullmatch(item)
                for item in value))


def _fingerprint(target: str | None, codes: list[str],
                 severity: str) -> str | None:
    if not codes:
        return None
    return base.sha256_obj({
        "target_session_date": target,
        "active_codes": codes,
        "severity": severity,
    })


def _state_payload(document: dict) -> dict:
    return {
        key: value for key, value in document.items()
        if key != "state_sha256"}


def _receipt_payload(document: dict) -> dict:
    return {
        key: value for key, value in document.items()
        if key != "artifact_sha256"}


def _initial_state(now: dt.datetime) -> dict:
    document = {
        "schema": STATE_SCHEMA,
        "updated_utc": now.isoformat(),
        "active_target_session_date": None,
        "active_fingerprint": None,
        "active_codes": [],
        "active_severity": "none",
        "last_transition": "initialized",
        "last_health_sha256": None,
        "notification_count": 0,
        "last_notification_utc": None,
        **SAFETY,
    }
    document["state_sha256"] = base.sha256_obj(document)
    return document


def validate_state(document: Any) -> list[str]:
    if not isinstance(document, dict) or document.get("schema") != STATE_SCHEMA:
        return ["state_schema_invalid"]
    errors: list[str] = []
    if set(document) != STATE_KEYS:
        errors.append("state_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"state_safety_invalid:{key}")
    if base.parse_utc(document.get("updated_utc")) is None:
        errors.append("state_updated_utc_invalid")
    target = document.get("active_target_session_date")
    if target is not None and (
            not isinstance(target, str) or not DATE_RE.fullmatch(target)):
        errors.append("state_target_invalid")
        target = None
    if not _codes_valid(document.get("active_codes")):
        errors.append("state_active_codes_invalid")
    codes = (
        document.get("active_codes")
        if isinstance(document.get("active_codes"), list) else [])
    severity = document.get("active_severity")
    if severity not in {"none", "warning", "critical"}:
        errors.append("state_severity_invalid")
        severity = "none"
    expected_fingerprint = _fingerprint(target, codes, severity)
    if document.get("active_fingerprint") != expected_fingerprint:
        errors.append("state_active_fingerprint_mismatch")
    if ((not codes and (severity != "none" or target is not None))
            or (codes and (severity == "none" or target is None))):
        errors.append("state_active_semantics_invalid")
    if document.get("last_transition") not in TRANSITIONS:
        errors.append("state_transition_invalid")
    if document.get("last_health_sha256") is not None \
            and _safe_sha(document.get("last_health_sha256")) is None:
        errors.append("state_health_sha_invalid")
    count = document.get("notification_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        errors.append("state_notification_count_invalid")
    last_notification = document.get("last_notification_utc")
    if (last_notification is not None
            and base.parse_utc(last_notification) is None):
        errors.append("state_last_notification_invalid")
    if document.get("state_sha256") != base.sha256_obj(_state_payload(document)):
        errors.append("state_sha256_mismatch")
    if _has_prohibited_key(document):
        errors.append("state_prohibited_key")
    return sorted(set(errors))


def _persistence_row(workflow: str, target: str, status: str,
                     root: Path) -> dict:
    """Durable, receipt-derived consecutive-miss count; never a mutable in-memory counter.

    Fails closed on its own errors: an unusable history reports streak 1 with
    `history_complete` false, so an escalation is never invented from a broken read.
    """
    try:
        return slo_persistence.consecutive_missed(
            workflow=workflow, target=target, current_status=status, root=root)
    except Exception:
        return {"workflow": workflow, "streak": 1, "sessions": [target],
                "diagnostics": ["history_evaluation_failed"], "history_complete": False,
                "history_conflict": True}


PERSISTENT_RH_REASON = "robinhood_collector_unavailable_persistent"


def _broker_read_path_persistent(root: Path) -> bool:
    """True when the durable marker carries the CRITICAL Robinhood read-path reason.

    Reads one local artifact. No broker call, no model, no network — this function can only
    ever make the alert louder, never quieter.
    """
    marker = Path(root) / "data" / "runtime" / "health" / "attention_required.json"
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(document, dict):
        return False
    active = document.get("active_reasons")
    if isinstance(active, dict) and PERSISTENT_RH_REASON in active:
        return True
    return document.get("reason") == PERSISTENT_RH_REASON


def _condition_codes(health: dict, policy: dict, target: str,
                     now: dt.datetime,
                     root: Path = ROOT) -> tuple[list[str], str, str | None, dict]:
    validation = autonomy_service_health.validate(health)
    if validation:
        return ["health_receipt_invalid"], "critical", None, {}
    codes: list[str] = []
    critical = False
    if health.get("target_session_date") != target:
        codes.append("health_target_mismatch")
        critical = True
    generated = base.parse_utc(health.get("generated_utc"))
    if generated is None:
        codes.append("health_timestamp_invalid")
        critical = True
    else:
        age = (now - generated.astimezone(dt.timezone.utc)).total_seconds()
        if age > 120 or age < -60:
            codes.append("health_receipt_stale")
            critical = True
    service_policy = health.get("service_slo_policy") or {}
    if service_policy.get("status") == "invalid":
        codes.append("service_slo_policy_invalid")
        critical = True
    if (policy["alert_on_scheduler_config_issue"]
            and (health.get("scheduler_config_health") or {}).get("status")
            != "healthy"):
        codes.append("scheduler_config_at_issue")
        critical = True
    service = health.get("service_slo_health") or {}
    persistence: dict[str, dict] = {}
    for name in WORKFLOWS:
        status = (service.get(name) or {}).get("status")
        if status == "missed" and policy["alert_on_missed_slo"]:
            # DURATION, not repetition: one missed morning is a bad day; two consecutive
            # missed EXPECTED trading sessions is a broken system, and must not keep the
            # fingerprint of a single bad day (2026-07-29..07-31 lost three sessions to
            # exactly that collision).
            row = _persistence_row(name, target, status, root)
            persistence[name] = row
            if slo_persistence.is_persistent(row["streak"]):
                codes.append(f"slo_missed_persistent:{name}")
                critical = True
            else:
                codes.append(f"slo_missed:{name}")
            if row.get("history_conflict"):
                # Evidence exists and contradicts itself. A plain ABSENCE of older receipts
                # is normal and stays out of the fingerprint; a corrupt or mis-filed one is
                # an integrity fault that must be as loud as the outage it could mask.
                codes.append(f"slo_history_conflict:{name}")
        elif status == "invalid" and policy["alert_on_invalid_slo"]:
            codes.append(f"slo_invalid:{name}")
            critical = True
    # 2026-08-04: this alert measured only whether the CRON FIRED and what its receipts looked
    # like — it had no dimension for what the run actually produced. On 2026-08-04 it reported
    # `clear` while the Buy verifier had been dead for ten consecutive trading sessions. A
    # persistently dead broker read path is now a first-class CRITICAL condition here, sourced
    # from the durable marker so the alert and the operator artifact cannot disagree.
    if _broker_read_path_persistent(root):
        codes.append("broker_read_path_persistent")
        critical = True

    research_artifacts = health.get("research_artifact_health") or {}
    for reason in research_artifacts.get("reason_codes") or []:
        if reason in {
                "candidate_artifact_invalid", "candidate_artifact_missing",
                "candidate_health_receipt_invalid",
                "candidate_quote_artifact_invalid",
                "candidate_quote_health_receipt_invalid",
                "post_open_skip_contradicts_official_morning"}:
            codes.append(reason)
        elif isinstance(reason, str) and reason.startswith(
                "post_open_dependency_resolution_invalid:"):
            codes.append(reason)
    severity = "critical" if critical else ("warning" if codes else "none")
    return sorted(set(codes)), severity, health.get("overall_status"), persistence


def _persistence_projection(persistence: dict | None) -> dict:
    """Operator-visible duration summary. Status tokens and counts only — no prose."""
    if not isinstance(persistence, dict):
        return {}
    projection: dict[str, dict] = {}
    for workflow, row in sorted(persistence.items()):
        if workflow not in WORKFLOWS or not isinstance(row, dict):
            continue
        streak = row.get("streak")
        sessions = row.get("sessions")
        diagnostics = row.get("diagnostics")
        projection[workflow] = {
            "consecutive_missed_sessions": streak if isinstance(streak, int) else 0,
            "sessions": [item for item in (sessions or []) if DATE_RE.fullmatch(str(item))][:12],
            "history_complete": row.get("history_complete") is True,
            "history_conflict": row.get("history_conflict") is True,
            "diagnostics": sorted({str(item) for item in (diagnostics or [])
                                   if CODE_RE.fullmatch(str(item))})[:12],
            "persistent": slo_persistence.is_persistent(
                streak if isinstance(streak, int) else 0),
        }
    return projection


def _make_receipt(*, now: dt.datetime, target: str | None,
                  status: str, transition: str, severity: str,
                  health_status: str | None, health_sha: str | None,
                  previous_fingerprint: str | None,
                  active_fingerprint: str | None, active_codes: list[str],
                  notification_required: bool, notification_kind: str,
                  state_written: bool, errors: list[str],
                  persistence: dict | None = None) -> dict:
    document = {
        "schema": RECEIPT_SCHEMA,
        "generated_utc": now.isoformat(),
        "target_session_date": target,
        "slo_persistence": _persistence_projection(persistence),
        "status": status,
        "transition": transition,
        "severity": severity,
        "health_overall_status": health_status,
        "health_artifact_sha256": health_sha,
        "previous_active_fingerprint": previous_fingerprint,
        "active_fingerprint": active_fingerprint,
        "active_codes": active_codes,
        "notification_required": notification_required,
        "notification_kind": notification_kind,
        "state_written": state_written,
        "errors": sorted(set(errors)),
        **SAFETY,
    }
    document["artifact_sha256"] = base.sha256_obj(document)
    return document


def validate_receipt(document: Any) -> list[str]:
    if not isinstance(document, dict) or document.get("schema") != RECEIPT_SCHEMA:
        return ["receipt_schema_invalid"]
    errors: list[str] = []
    if set(document) != RECEIPT_KEYS:
        errors.append("receipt_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"receipt_safety_invalid:{key}")
    if base.parse_utc(document.get("generated_utc")) is None:
        errors.append("receipt_generated_utc_invalid")
    target = document.get("target_session_date")
    if target is not None and (
            not isinstance(target, str) or not DATE_RE.fullmatch(target)):
        errors.append("receipt_target_invalid")
        target = None
    if document.get("status") not in {
            "alert", "unchanged", "resolved", "clear", "unavailable", "blocked"}:
        errors.append("receipt_status_invalid")
    if document.get("transition") not in {
            "new_alert", "changed_alert", "unchanged", "resolved", "clear",
            "unavailable", "state_invalid"}:
        errors.append("receipt_transition_invalid")
    severity = document.get("severity")
    if severity not in {"none", "warning", "critical"}:
        errors.append("receipt_severity_invalid")
        severity = "none"
    if document.get("health_overall_status") not in {
            "healthy", "degraded", "in_progress", "at_issue", None}:
        errors.append("receipt_health_status_invalid")
    for key in (
            "health_artifact_sha256", "previous_active_fingerprint",
            "active_fingerprint"):
        if document.get(key) is not None and _safe_sha(document.get(key)) is None:
            errors.append(f"receipt_sha_invalid:{key}")
    if not _codes_valid(document.get("active_codes")):
        errors.append("receipt_active_codes_invalid")
    codes = (
        document.get("active_codes")
        if isinstance(document.get("active_codes"), list) else [])
    if document.get("active_fingerprint") != _fingerprint(target, codes, severity):
        errors.append("receipt_active_fingerprint_mismatch")
    if ((not codes and severity != "none")
            or (codes and (severity == "none" or target is None))):
        errors.append("receipt_active_semantics_invalid")
    if not isinstance(document.get("notification_required"), bool):
        errors.append("receipt_notification_required_invalid")
    if document.get("notification_kind") not in {"none", "alert", "recovery"}:
        errors.append("receipt_notification_kind_invalid")
    if not isinstance(document.get("state_written"), bool):
        errors.append("receipt_state_written_invalid")
    receipt_errors = document.get("errors")
    if (not isinstance(receipt_errors, list)
            or receipt_errors != sorted(set(receipt_errors))
            or any(not isinstance(item, str) or not CODE_RE.fullmatch(item)
                   for item in receipt_errors)):
        errors.append("receipt_errors_invalid")
    if document.get("status") == "alert" and (
            document.get("transition") not in {"new_alert", "changed_alert"}
            or document.get("notification_required") is not True
            or document.get("notification_kind") != "alert"):
        errors.append("receipt_alert_semantics_invalid")
    if document.get("status") == "unchanged" and (
            document.get("transition") != "unchanged"
            or document.get("notification_required") is not False):
        errors.append("receipt_unchanged_semantics_invalid")
    if document.get("status") == "resolved" and (
            document.get("transition") != "resolved"):
        errors.append("receipt_resolved_semantics_invalid")
    if document.get("status") == "unavailable" \
            and document.get("state_written") is not False:
        errors.append("receipt_unavailable_state_write_invalid")
    if document.get("status") == "blocked" \
            and document.get("transition") != "state_invalid":
        errors.append("receipt_blocked_semantics_invalid")
    persistence = document.get("slo_persistence")
    if not isinstance(persistence, dict):
        errors.append("receipt_persistence_invalid")
    else:
        for workflow, row in persistence.items():
            if workflow not in WORKFLOWS or not isinstance(row, dict) or set(row) != {
                    "consecutive_missed_sessions", "sessions", "history_complete",
                    "history_conflict", "diagnostics", "persistent"}:
                errors.append("receipt_persistence_invalid")
                break
            streak = row.get("consecutive_missed_sessions")
            if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
                errors.append("receipt_persistence_streak_invalid")
            if not isinstance(row.get("history_complete"), bool) \
                    or not isinstance(row.get("history_conflict"), bool) \
                    or not isinstance(row.get("persistent"), bool):
                errors.append("receipt_persistence_boolean_invalid")
            sessions = row.get("sessions")
            if not isinstance(sessions, list) or any(
                    not isinstance(item, str) or not DATE_RE.fullmatch(item)
                    for item in sessions):
                errors.append("receipt_persistence_sessions_invalid")
            diagnostics = row.get("diagnostics")
            if not isinstance(diagnostics, list) or diagnostics != sorted(set(diagnostics)) \
                    or any(not isinstance(item, str) or not CODE_RE.fullmatch(item)
                           for item in diagnostics):
                errors.append("receipt_persistence_diagnostics_invalid")
            if row.get("persistent") is not slo_persistence.is_persistent(
                    streak if isinstance(streak, int) and not isinstance(streak, bool) else 0):
                errors.append("receipt_persistence_semantics_invalid")
    if document.get("artifact_sha256") != base.sha256_obj(
            _receipt_payload(document)):
        errors.append("receipt_sha256_mismatch")
    if _has_prohibited_key(document):
        errors.append("receipt_prohibited_key")
    return sorted(set(errors))


def evaluate(*, health: dict | None, previous: dict, policy: dict,
             target: str, now: dt.datetime,
             root: Path = ROOT) -> tuple[dict, dict | None]:
    previous_fingerprint = previous["active_fingerprint"]
    if health is None:
        return _make_receipt(
            now=now, target=target, status="unavailable",
            transition="unavailable", severity="none", health_status=None,
            health_sha=None, previous_fingerprint=previous_fingerprint,
            active_fingerprint=None, active_codes=[],
            notification_required=False, notification_kind="none",
            state_written=False, errors=["health_missing_or_unreadable"]), None

    codes, severity, health_status, persistence = _condition_codes(
        health, policy, target, now, root)
    health_sha = _safe_sha(health.get("artifact_sha256"))
    active_fingerprint = _fingerprint(target, codes, severity)
    if active_fingerprint is not None:
        if previous_fingerprint is None:
            transition = "new_alert"
        elif previous_fingerprint != active_fingerprint:
            transition = "changed_alert"
        else:
            transition = "unchanged"
        notification_required = transition in {"new_alert", "changed_alert"}
        status = "alert" if notification_required else "unchanged"
        notification_kind = "alert" if notification_required else "none"
    else:
        transition = "resolved" if previous_fingerprint is not None else "clear"
        status = "resolved" if transition == "resolved" else "clear"
        notification_required = bool(
            transition == "resolved" and policy["notify_on_recovery"])
        notification_kind = "recovery" if notification_required else "none"

    notification_count = (
        previous["notification_count"] + int(notification_required))
    next_state = {
        "schema": STATE_SCHEMA,
        "updated_utc": now.isoformat(),
        "active_target_session_date": target if codes else None,
        "active_fingerprint": active_fingerprint,
        "active_codes": codes,
        "active_severity": severity,
        "last_transition": transition,
        "last_health_sha256": health_sha,
        "notification_count": notification_count,
        "last_notification_utc": (
            now.isoformat() if notification_required
            else previous["last_notification_utc"]),
        **SAFETY,
    }
    next_state["state_sha256"] = base.sha256_obj(next_state)
    receipt = _make_receipt(
        now=now, target=target, status=status, transition=transition,
        severity=severity, health_status=health_status, health_sha=health_sha,
        previous_fingerprint=previous_fingerprint,
        active_fingerprint=active_fingerprint, active_codes=codes,
        notification_required=notification_required,
        notification_kind=notification_kind, state_written=False, errors=[],
        persistence=persistence)
    return receipt, next_state


def _read_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write(path: Path, document: dict) -> None:
    if path.is_symlink():
        raise ValueError("output_symlink_refused")
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
        with contextlib.suppress(OSError):
            os.unlink(temporary)


@contextlib.contextmanager
def _state_lock(state_path: Path) -> Iterator[None]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.parent / f".{state_path.name}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_now(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    parsed = base.parse_utc(value)
    if parsed is None:
        raise ValueError("now_invalid")
    return parsed.astimezone(dt.timezone.utc)


def _state_invalid_receipt(now: dt.datetime, target: str,
                           errors: list[str]) -> dict:
    codes = ["alert_state_invalid"]
    return _make_receipt(
        now=now, target=target, status="blocked",
        transition="state_invalid", severity="critical", health_status=None,
        health_sha=None, previous_fingerprint=None,
        active_fingerprint=_fingerprint(target, codes, "critical"),
        active_codes=codes, notification_required=False,
        notification_kind="none", state_written=False, errors=errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicated local autonomy service alert decisions.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--target")
    parser.add_argument("--health")
    parser.add_argument("--state")
    parser.add_argument("--out")
    parser.add_argument("--now")
    parser.add_argument("--root", default=str(ROOT),
                        help="Repository root used to resolve the SLO-miss history.")
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    if args.check:
        result = cross_validate(config_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    try:
        if not isinstance(args.target, str) or not DATE_RE.fullmatch(args.target):
            raise ValueError("target_invalid")
        policy, policy_errors = load_policy(config_path)
        if policy is None or policy_errors:
            raise ValueError("policy_invalid:" + ",".join(policy_errors))
        now = _parse_now(args.now)
        health_path = Path(args.health or (
            ROOT / "data" / "runtime" / "health" /
            f"{args.target}_autonomy_service_health.json"))
        state_path = Path(args.state or (ROOT / policy["state_path"]))
        receipt_path = Path(args.out or (ROOT / policy["receipt_path"]))
        health = _read_object(health_path)
        with _state_lock(state_path):
            if state_path.exists():
                previous = _read_object(state_path)
                state_errors = validate_state(previous)
                if state_errors:
                    receipt = _state_invalid_receipt(
                        now, args.target, state_errors)
                    _atomic_write(receipt_path, receipt)
                    print(json.dumps(receipt, indent=2, sort_keys=True))
                    return 2
            else:
                previous = _initial_state(now)
            receipt, next_state = evaluate(
                health=health, previous=previous, policy=policy,
                target=args.target, now=now, root=Path(args.root))
            if next_state is not None:
                state_errors = validate_state(next_state)
                if state_errors:
                    raise ValueError(
                        "next_state_self_validation_failed:"
                        + ",".join(state_errors))
                _atomic_write(state_path, next_state)
                receipt["state_written"] = True
                receipt["artifact_sha256"] = base.sha256_obj(
                    _receipt_payload(receipt))
            receipt_errors = validate_receipt(receipt)
            if receipt_errors:
                raise ValueError(
                    "receipt_self_validation_failed:"
                    + ",".join(receipt_errors))
            _atomic_write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        if (receipt["notification_kind"] == "alert"
                and receipt["notification_required"]):
            return 10
        if (receipt["notification_kind"] == "recovery"
                and receipt["notification_required"]):
            return 11
        return 0
    except Exception as exc:
        print(json.dumps(
            {"ok": False, "error": str(exc), **SAFETY},
            indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
