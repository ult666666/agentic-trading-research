#!/usr/bin/env python3
"""Append-only strategy comparison registry for the offline quant lab.

The registry freezes which benchmark and incumbent references existed before a candidate's
uniqueness evaluation. Every event is hash chained and append-only. Source portfolio paths and
hashes are captured at registration and can never be rebound. Role transitions require a
separate approval reference and are visible to later cohorts only.

This module grants no production, activation, profitability, broker, account, or order authority.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import features as qf  # noqa: E402
from quant import strategy_uniqueness as su  # noqa: E402

TOOL = "quant.strategy_registry"
POLICY_SCHEMA = "quant_strategy_registry_policy/1.0"
EVENT_SCHEMA = "quant_strategy_registry_event/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_registry_policy.json"
REGISTRY_DEFAULT = ROOT / "data" / "quant_strategy_registry.jsonl"
_MAX_BYTES = 64 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NUM = (int, float)

ROLES = (
    "benchmark_reference",
    "incumbent_reference",
    "candidate_review",
    "retired_reference",
)
REGISTER_ROLES = ROLES[:-1]
TRANSITION_REASONS = {
    "category_D_approved_for_incumbent_library",
    "reference_retired",
    "benchmark_replaced",
}

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "feeds_research_factory": False,
    "strategy_change_allowed": False,
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
}

EVENT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "feeds_research_factory": False,
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
}

OUTPUT_SAFETY = {
    **EVENT_SAFETY,
    "history_rewrite_allowed": False,
    "source_rebinding_allowed": False,
}

_POLICY_KEYS = {
    "schema", "version", "registry_id", "source_artifact_schema",
    "allowed_source_roots", "roles", "transition_rules", "minimum_incumbents",
    "maximum_incumbents", "benchmark_selection_rule", "incumbent_selection_rule",
    "candidate_lock_rule", "source_binding_rule", "source_contract_rule",
    "date_alignment_rule", "append_rule", "history_rewrite_allowed",
    "source_rebinding_allowed", "automatic_transition_allowed",
    "transition_requires_approval_reference", "safety",
}

_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "version": "1.0.0",
    "registry_id": "charles-quant-strategy-library",
    "source_artifact_schema": su.pb.RESULT_SCHEMA,
    "allowed_source_roots": [
        "data/runtime/experiments",
        "data/runtime/quant/strategy_artifacts",
    ],
    "roles": list(ROLES),
    "transition_rules": {
        "benchmark_reference": ["retired_reference"],
        "candidate_review": ["incumbent_reference", "retired_reference"],
        "incumbent_reference": ["retired_reference"],
        "retired_reference": [],
    },
    "minimum_incumbents": 1,
    "maximum_incumbents": 20,
    "benchmark_selection_rule": (
        "exactly_one_benchmark_reference_at_candidate_registration_sequence"),
    "incumbent_selection_rule": (
        "all_incumbent_references_at_candidate_registration_sequence_no_discretionary_omission"),
    "candidate_lock_rule": "candidate_review_registration_event_sequence",
    "source_binding_rule": (
        "repo_relative_path_plus_content_and_result_sha256_captured_at_registration"),
    "source_contract_rule": (
        "matching_cost_stress_cost_policy_and_portfolio_policy_required"),
    "date_alignment_rule": (
        "common_date_intersection_without_imputation_subject_to_uniqueness_policy"),
    "append_rule": "exclusive_lock_validate_head_append_one_fsync",
    "history_rewrite_allowed": False,
    "source_rebinding_allowed": False,
    "automatic_transition_allowed": False,
    "transition_requires_approval_reference": True,
}

_EVENT_KEYS = set(EVENT_SAFETY) | {
    "tool", "schema", "registry_id", "sequence", "event_type", "occurred_at_utc",
    "previous_event_sha256", "policy_sha256", "payload", "event_sha256",
}
_GENESIS_KEYS = {"policy_version", "note"}
_GENESIS_NOTE = "Offline comparison registry genesis; grants no strategy or execution authority."
_REGISTER_KEYS = {
    "strategy_id", "role", "hypothesis_family", "feature_family", "source_path",
    "source_result_sha256", "source_content_sha256", "evaluation_start_date",
    "evaluation_end_date", "cost_stress_multiplier", "cost_policy_version",
    "portfolio_policy_version", "cost_policy_sha256", "portfolio_policy_sha256",
    "evidence_reference",
}
_TRANSITION_KEYS = {
    "strategy_id", "from_role", "to_role", "reason_code", "approval_reference",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload(document: dict, hash_key: str) -> dict:
    return {key: value for key, value in document.items() if key != hash_key}


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _refuse(error: str, **extra: Any) -> dict:
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["registry_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("registry_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"registry_policy_rule_invalid:{key}")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("registry_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise ValueError("policy_not_regular_bounded_file")
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, ["registry_policy_unreadable"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    return {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": policy is not None and not errors,
        "valid": policy is not None and not errors,
        "errors": errors,
        "version": policy.get("version") if isinstance(policy, dict) else None,
        "policy_sha256": sha256_obj(policy) if isinstance(policy, dict) else None,
    }


def _safe_source_path(value: Any, policy: dict) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return False
    return any(path == Path(root) or Path(root) in path.parents
               for root in policy["allowed_source_roots"])


def resolve_source(value: str, repository_root: Path, policy: dict) -> Path:
    if not _safe_source_path(value, policy):
        raise ValueError("source_path_not_allowlisted_relative")
    if repository_root.is_symlink() or not repository_root.is_dir():
        raise ValueError("repository_root_invalid")
    root = repository_root.resolve()
    lexical = root / value
    current = root
    for part in Path(value).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("source_parent_symlink_not_allowed")
    if not lexical.is_file():
        raise ValueError("source_not_regular_file")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("source_escapes_repository_root") from exc
    return resolved


def _event(event_type: str, occurred_at_utc: str, payload: dict, events: list[dict],
           policy: dict) -> dict:
    document = {
        **EVENT_SAFETY,
        "tool": TOOL,
        "schema": EVENT_SCHEMA,
        "registry_id": policy["registry_id"],
        "sequence": len(events),
        "event_type": event_type,
        "occurred_at_utc": occurred_at_utc,
        "previous_event_sha256": events[-1]["event_sha256"] if events else None,
        "policy_sha256": sha256_obj(policy),
        "payload": payload,
        "event_sha256": "",
    }
    document["event_sha256"] = sha256_obj(_payload(document, "event_sha256"))
    return document


def build_genesis(policy: dict, occurred_at_utc: str) -> dict:
    errors = validate_policy(policy)
    if errors:
        return _refuse("registry_policy_invalid", errors=errors)
    if parse_utc(occurred_at_utc) is None:
        return _refuse("occurred_at_utc_invalid")
    return _event("genesis", occurred_at_utc, {
        "policy_version": policy["version"],
        "note": _GENESIS_NOTE,
    }, [], policy)


def _validate_register_payload(payload: Any, policy: dict, tag: str) -> list[str]:
    if not isinstance(payload, dict) or set(payload) != _REGISTER_KEYS:
        return [f"registry_register_payload_keys_invalid:{tag}"]
    errors: list[str] = []
    if not isinstance(payload.get("strategy_id"), str) or not _ID_RE.fullmatch(
            payload.get("strategy_id", "")):
        errors.append(f"registry_register_token_invalid:{tag}:strategy_id")
    for key in ("hypothesis_family", "feature_family", "evidence_reference"):
        if not isinstance(payload.get(key), str) or not _TOKEN_RE.fullmatch(payload.get(key, "")):
            errors.append(f"registry_register_token_invalid:{tag}:{key}")
    if payload.get("role") not in REGISTER_ROLES:
        errors.append(f"registry_register_role_invalid:{tag}")
    if not _safe_source_path(payload.get("source_path"), policy):
        errors.append(f"registry_register_source_path_invalid:{tag}")
    for key in ("source_result_sha256", "source_content_sha256", "cost_policy_sha256",
                "portfolio_policy_sha256"):
        if not isinstance(payload.get(key), str) or not _SHA_RE.fullmatch(payload.get(key, "")):
            errors.append(f"registry_register_hash_invalid:{tag}:{key}")
    if (not _valid_date(payload.get("evaluation_start_date"))
            or not _valid_date(payload.get("evaluation_end_date"))
            or payload.get("evaluation_start_date", "z") > payload.get(
                "evaluation_end_date", "")):
        errors.append(f"registry_register_evaluation_window_invalid:{tag}")
    if not _number(payload.get("cost_stress_multiplier")) or float(
            payload.get("cost_stress_multiplier", 0)) < 1:
        errors.append(f"registry_register_cost_stress_invalid:{tag}")
    for key in ("cost_policy_version", "portfolio_policy_version"):
        if not isinstance(payload.get(key), str) or not _SEMVER_RE.fullmatch(
                payload.get(key, "")):
            errors.append(f"registry_register_version_invalid:{tag}:{key}")
    return errors


def _validate_transition_payload(payload: Any, policy: dict, tag: str) -> list[str]:
    if not isinstance(payload, dict) or set(payload) != _TRANSITION_KEYS:
        return [f"registry_transition_payload_keys_invalid:{tag}"]
    errors: list[str] = []
    for key in ("strategy_id", "approval_reference"):
        if not isinstance(payload.get(key), str) or not _TOKEN_RE.fullmatch(
                payload.get(key, "")):
            errors.append(f"registry_transition_token_invalid:{tag}:{key}")
    if payload.get("from_role") not in ROLES or payload.get("to_role") not in ROLES:
        errors.append(f"registry_transition_role_invalid:{tag}")
    elif payload["to_role"] not in policy["transition_rules"][payload["from_role"]]:
        errors.append(f"registry_transition_not_allowed:{tag}")
    if payload.get("reason_code") not in TRANSITION_REASONS:
        errors.append(f"registry_transition_reason_invalid:{tag}")
    if (payload.get("to_role") == "incumbent_reference"
            and payload.get("reason_code") != "category_D_approved_for_incumbent_library"):
        errors.append(f"registry_transition_incumbent_reason_invalid:{tag}")
    return errors


def validate_events(events: Any, policy: dict) -> list[str]:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return [f"policy:{value}" for value in policy_errors]
    if not isinstance(events, list) or not events:
        return ["registry_events_missing"]
    errors: list[str] = []
    previous_hash = None
    previous_time = None
    states: dict[str, dict] = {}
    for index, event in enumerate(events):
        tag = str(index)
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            errors.append(f"registry_event_keys_invalid:{tag}")
            continue
        for key, expected in EVENT_SAFETY.items():
            if event.get(key) != expected:
                errors.append(f"registry_event_safety_invalid:{tag}:{key}")
        if (event.get("tool") != TOOL or event.get("schema") != EVENT_SCHEMA
                or event.get("registry_id") != policy["registry_id"]):
            errors.append(f"registry_event_identity_invalid:{tag}")
        if event.get("sequence") != index:
            errors.append(f"registry_event_sequence_invalid:{tag}")
        if event.get("previous_event_sha256") != previous_hash:
            errors.append(f"registry_event_previous_hash_invalid:{tag}")
        if event.get("policy_sha256") != sha256_obj(policy):
            errors.append(f"registry_event_policy_hash_invalid:{tag}")
        parsed = parse_utc(event.get("occurred_at_utc"))
        if parsed is None:
            errors.append(f"registry_event_time_invalid:{tag}")
        elif previous_time is not None and parsed < previous_time:
            errors.append(f"registry_event_time_not_monotonic:{tag}")
        else:
            previous_time = parsed
        if event.get("event_sha256") != sha256_obj(_payload(event, "event_sha256")):
            errors.append(f"registry_event_sha256_mismatch:{tag}")
        event_type = event.get("event_type")
        payload = event.get("payload")
        if index == 0:
            if (event_type != "genesis" or not isinstance(payload, dict) or set(
                    payload) != _GENESIS_KEYS or payload.get("policy_version") != policy["version"]
                    or payload.get("note") != _GENESIS_NOTE):
                errors.append("registry_genesis_invalid")
        elif event_type == "genesis":
            errors.append(f"registry_duplicate_genesis:{tag}")
        elif event_type == "register_strategy":
            errors.extend(_validate_register_payload(payload, policy, tag))
            if isinstance(payload, dict):
                strategy_id = payload.get("strategy_id")
                if strategy_id in states:
                    errors.append(f"registry_strategy_duplicate:{tag}:{strategy_id}")
                elif isinstance(strategy_id, str):
                    states[strategy_id] = {
                        "role": payload.get("role"),
                        "registration_sequence": index,
                        "registration_event_sha256": event.get("event_sha256"),
                        "registration_payload": payload,
                    }
                if parsed is not None and _valid_date(payload.get("evaluation_end_date")) and (
                        dt.date.fromisoformat(payload["evaluation_end_date"]) > parsed.date()):
                    errors.append(f"registry_source_window_after_registration:{tag}")
        elif event_type == "transition_role":
            errors.extend(_validate_transition_payload(payload, policy, tag))
            if isinstance(payload, dict):
                strategy_id = payload.get("strategy_id")
                state = states.get(strategy_id)
                if state is None:
                    errors.append(f"registry_transition_unknown_strategy:{tag}:{strategy_id}")
                elif state["role"] != payload.get("from_role"):
                    errors.append(f"registry_transition_from_role_mismatch:{tag}:{strategy_id}")
                else:
                    state["role"] = payload.get("to_role")
                    state["last_transition_sequence"] = index
        else:
            errors.append(f"registry_event_type_invalid:{tag}")
        previous_hash = event.get("event_sha256") if isinstance(
            event.get("event_sha256"), str) else None
    return sorted(set(errors))


def parse_registry_bytes(raw: bytes, policy: dict) -> tuple[list[dict], list[str]]:
    if not raw or len(raw) > _MAX_BYTES:
        return [], ["registry_file_empty_or_too_large"]
    if not raw.endswith(b"\n"):
        return [], ["registry_file_missing_terminal_newline"]
    events = []
    errors = []
    for index, line in enumerate(raw.splitlines()):
        if not line:
            errors.append(f"registry_blank_line:{index}")
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except Exception:
            errors.append(f"registry_line_malformed:{index}")
            continue
        events.append(event)
    if errors:
        return events, errors
    return events, validate_events(events, policy)


def read_registry(path: Path, policy: dict) -> tuple[list[dict], list[str]]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise ValueError("registry_not_regular_bounded_file")
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            raw = handle.read(_MAX_BYTES + 1)
    except Exception:
        return [], ["registry_unreadable"]
    return parse_registry_bytes(raw, policy)


def replay(events: list[dict], through_sequence: int | None = None) -> dict[str, dict]:
    states: dict[str, dict] = {}
    maximum = len(events) - 1 if through_sequence is None else through_sequence
    for event in events:
        if event["sequence"] > maximum:
            break
        payload = event["payload"]
        if event["event_type"] == "register_strategy":
            states[payload["strategy_id"]] = {
                "strategy_id": payload["strategy_id"],
                "role": payload["role"],
                "registration_sequence": event["sequence"],
                "registration_event_sha256": event["event_sha256"],
                "registration_time_utc": event["occurred_at_utc"],
                "registration_payload": payload,
            }
        elif event["event_type"] == "transition_role":
            state = states[payload["strategy_id"]]
            state["role"] = payload["to_role"]
            state["last_transition_sequence"] = event["sequence"]
            state["last_transition_time_utc"] = event["occurred_at_utc"]
    return states


def build_register_event(events: list[dict], policy: dict, repository_root: Path,
                         strategy_id: str, role: str, hypothesis_family: str,
                         feature_family: str, source_path: str, evidence_reference: str,
                         occurred_at_utc: str) -> dict:
    existing_errors = validate_events(events, policy)
    if existing_errors:
        return _refuse("registry_invalid_before_append", errors=existing_errors)
    if (not _ID_RE.fullmatch(strategy_id or "") or role not in REGISTER_ROLES
            or not _TOKEN_RE.fullmatch(hypothesis_family or "")
            or not _TOKEN_RE.fullmatch(feature_family or "")
            or not _TOKEN_RE.fullmatch(evidence_reference or "")):
        return _refuse("registration_argument_invalid")
    if strategy_id in replay(events):
        return _refuse("strategy_already_registered")
    occurred = parse_utc(occurred_at_utc)
    if occurred is None or occurred < parse_utc(events[-1]["occurred_at_utc"]):
        return _refuse("occurred_at_utc_invalid_or_before_head")
    try:
        source = resolve_source(source_path, repository_root, policy)
        document = su._read(source)
    except Exception as exc:
        return _refuse(f"source_unreadable:{type(exc).__name__}:{exc}")
    source_errors = su._portfolio_errors(document, "registry_source")
    if source_errors:
        return _refuse("source_portfolio_invalid", errors=source_errors)
    daily = document["daily_path"]
    if dt.date.fromisoformat(daily[-1]["date"]) > occurred.date():
        return _refuse("source_window_after_registration")
    payload = {
        "strategy_id": strategy_id,
        "role": role,
        "hypothesis_family": hypothesis_family,
        "feature_family": feature_family,
        "source_path": source_path,
        "source_result_sha256": document["result_sha256"],
        "source_content_sha256": su.sha256_obj(document),
        "evaluation_start_date": daily[0]["date"],
        "evaluation_end_date": daily[-1]["date"],
        "cost_stress_multiplier": float(document["cost_stress_multiplier"]),
        "cost_policy_version": document["policy_versions"]["cost"],
        "portfolio_policy_version": document["policy_versions"]["portfolio"],
        "cost_policy_sha256": document["lineage"]["cost_policy_sha256"],
        "portfolio_policy_sha256": document["lineage"]["portfolio_policy_sha256"],
        "evidence_reference": evidence_reference,
    }
    event = _event("register_strategy", occurred_at_utc, payload, events, policy)
    errors = validate_events([*events, event], policy)
    return event if not errors else _refuse("registration_event_self_validation_failed",
                                            errors=errors)


def build_transition_event(events: list[dict], policy: dict, strategy_id: str,
                           to_role: str, reason_code: str, approval_reference: str,
                           occurred_at_utc: str) -> dict:
    existing_errors = validate_events(events, policy)
    if existing_errors:
        return _refuse("registry_invalid_before_append", errors=existing_errors)
    states = replay(events)
    state = states.get(strategy_id)
    if state is None:
        return _refuse("strategy_not_registered")
    if (to_role not in policy["transition_rules"].get(state["role"], [])
            or reason_code not in TRANSITION_REASONS
            or not _TOKEN_RE.fullmatch(approval_reference or "")):
        return _refuse("transition_argument_invalid")
    occurred = parse_utc(occurred_at_utc)
    if occurred is None or occurred < parse_utc(events[-1]["occurred_at_utc"]):
        return _refuse("occurred_at_utc_invalid_or_before_head")
    payload = {
        "strategy_id": strategy_id,
        "from_role": state["role"],
        "to_role": to_role,
        "reason_code": reason_code,
        "approval_reference": approval_reference,
    }
    event = _event("transition_role", occurred_at_utc, payload, events, policy)
    errors = validate_events([*events, event], policy)
    return event if not errors else _refuse("transition_event_self_validation_failed",
                                            errors=errors)


def create_registry(path: Path, genesis: dict, policy: dict) -> dict:
    if path.exists():
        return _refuse("registry_already_exists")
    errors = validate_events([genesis], policy)
    if errors:
        return _refuse("genesis_invalid", errors=errors)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_bytes(genesis) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("registry_already_exists")
        os.replace(temporary, path)
        temporary = ""
    except Exception as exc:
        return _refuse(f"registry_create_failed:{type(exc).__name__}:{exc}")
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": True, "registry": str(path),
            "event_count": 1, "head_event_sha256": genesis["event_sha256"]}


def append_event(path: Path, event: dict, policy: dict) -> dict:
    if path.is_symlink() or not path.is_file():
        return _refuse("registry_not_regular_file")
    try:
        with path.open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            raw = handle.read(_MAX_BYTES + 1)
            events, errors = parse_registry_bytes(raw, policy)
            if errors:
                return _refuse("registry_invalid_before_append", errors=errors)
            if (event.get("sequence") != len(events)
                    or event.get("previous_event_sha256") != events[-1]["event_sha256"]):
                return _refuse("registry_head_changed_retry_required")
            combined_errors = validate_events([*events, event], policy)
            if combined_errors:
                return _refuse("event_invalid", errors=combined_errors)
            handle.seek(0, os.SEEK_END)
            handle.write(canonical_bytes(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        return _refuse(f"registry_append_failed:{type(exc).__name__}:{exc}")
    return {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "registry": str(path),
        "event_count": event["sequence"] + 1,
        "head_event_sha256": event["event_sha256"],
        "appended_event_type": event["event_type"],
    }


def _summary(path: Path, policy: dict) -> dict:
    events, errors = read_registry(path, policy)
    states = replay(events) if not errors else {}
    role_counts = {role: sum(state["role"] == role for state in states.values())
                   for role in ROLES}
    return {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": not errors,
        "valid": not errors,
        "errors": errors,
        "registry": str(path),
        "event_count": len(events),
        "head_event_sha256": events[-1].get("event_sha256") if events else None,
        "strategy_count": len(states),
        "role_counts": role_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append-only offline strategy registry.")
    parser.add_argument("--policy", default=str(POLICY_DEFAULT))
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--registry", default=str(REGISTRY_DEFAULT))
    init = sub.add_parser("init")
    init.add_argument("--registry", required=True)
    init.add_argument("--occurred-at", required=True)
    register = sub.add_parser("register")
    register.add_argument("--registry", default=str(REGISTRY_DEFAULT))
    register.add_argument("--repo-root", default=str(ROOT))
    register.add_argument("--strategy-id", required=True)
    register.add_argument("--role", required=True, choices=REGISTER_ROLES)
    register.add_argument("--hypothesis-family", required=True)
    register.add_argument("--feature-family", required=True)
    register.add_argument("--source-path", required=True)
    register.add_argument("--evidence-reference", required=True)
    register.add_argument("--occurred-at", required=True)
    transition = sub.add_parser("transition")
    transition.add_argument("--registry", default=str(REGISTRY_DEFAULT))
    transition.add_argument("--strategy-id", required=True)
    transition.add_argument("--to-role", required=True, choices=ROLES)
    transition.add_argument("--reason-code", required=True, choices=sorted(TRANSITION_REASONS))
    transition.add_argument("--approval-reference", required=True)
    transition.add_argument("--occurred-at", required=True)
    args = parser.parse_args(argv)
    policy, policy_errors = load_policy(Path(args.policy))
    if policy is None or policy_errors:
        output = _refuse("registry_policy_invalid", errors=policy_errors)
    elif args.command == "check":
        output = _summary(Path(args.registry), policy)
    elif args.command == "init":
        genesis = build_genesis(policy, args.occurred_at)
        output = (genesis if not genesis.get("event_sha256") else
                  create_registry(Path(args.registry), genesis, policy))
    else:
        events, errors = read_registry(Path(args.registry), policy)
        if errors:
            output = _refuse("registry_invalid_before_append", errors=errors)
        elif args.command == "register":
            event = build_register_event(
                events, policy, Path(args.repo_root), args.strategy_id, args.role,
                args.hypothesis_family, args.feature_family, args.source_path,
                args.evidence_reference, args.occurred_at)
            output = event if event.get("ok") is False else append_event(
                Path(args.registry), event, policy)
        else:
            event = build_transition_event(
                events, policy, args.strategy_id, args.to_role, args.reason_code,
                args.approval_reference, args.occurred_at)
            output = event if event.get("ok") is False else append_event(
                Path(args.registry), event, policy)
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
