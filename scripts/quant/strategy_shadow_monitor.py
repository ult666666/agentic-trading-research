#!/usr/bin/env python3
"""Prospective, non-backfilled strategy shadow observation.

An eligible strategy-evidence dossier may be frozen into a human-approved observation plan.
Future session observations are then appended to a hash-chained ledger and evaluated only at
pre-registered prefixes. This component is offline research infrastructure: it cannot register,
promote, activate, trade, access a broker, or establish profitability.
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
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_market_day  # noqa: E402
from quant import inference_audit  # noqa: E402
from quant import portfolio_backtest  # noqa: E402
from quant import strategy_artifacts  # noqa: E402
from quant import strategy_cohort as cohort  # noqa: E402
from quant import strategy_evidence_gate as evidence_gate  # noqa: E402
from quant import strategy_uniqueness as uniqueness  # noqa: E402

TOOL = "quant.strategy_shadow_monitor"
POLICY_SCHEMA = "quant_strategy_shadow_policy/1.0"
PLAN_SCHEMA = "quant_strategy_shadow_plan/1.0"
STRATEGY_CONTRACT_SCHEMA = "quant_strategy_shadow_strategy_contract/1.1"
RETURN_BUNDLE_SCHEMA = "quant_strategy_shadow_return_bundle/1.0"
DRAFT_SCHEMA = "quant_strategy_shadow_observation_draft/1.0"
EVENT_SCHEMA = "quant_strategy_shadow_event/1.0"
SUMMARY_SCHEMA = "quant_strategy_shadow_summary/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_shadow_policy.json"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NUM = (int, float)
_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "feeds_production": False,
    "feeds_research_factory": False,
    "feeds_buy_gate": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "automatic_shadow_start_allowed": False,
    "registry_mutation_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
    "model_invoked": False,
}

LIMITATIONS = [
    "Shadow returns are hypothetical model observations, not realized account performance or verified fills.",
    "Positive checkpoint evidence does not establish future profitability, capacity, causality, or regime persistence.",
    "HAC intervals depend on weak-dependence and asymptotic assumptions and can remain unreliable in small samples.",
    "The incumbent comparison is complete only to the frozen evidence dossier and cannot reveal omitted strategies.",
    "Modeled costs may differ from future spreads, impact, slippage, halts, gaps, taxes, and manual execution.",
    "A human must separately review any result; this component grants no production, Buy, broker, or execution authority.",
]

_POLICY_KEYS = {
    "schema", "version", "objective", "evidence_gate_schema", "plan_schema",
    "return_bundle_schema", "draft_schema", "event_schema", "summary_schema",
    "required_evidence_gate_status",
    "allowed_input_roots", "output_root", "descriptive_checkpoints_sessions",
    "decision_checkpoint_alpha_schedule", "maximum_familywise_alpha",
    "minimum_complete_fraction", "minimum_candidate_active_fraction",
    "minimum_hac_observations", "minimum_candidate_total_return",
    "minimum_benchmark_excess_hac_lower_bound", "minimum_incumbent_excess_hac_lower_bound",
    "maximum_absolute_incumbent_correlation", "maximum_active_day_jaccard",
    "maximum_shadow_drawdown", "maximum_absolute_daily_return", "maximum_gross_exposure",
    "maximum_observation_sessions", "maximum_recording_delay_hours_for_complete",
    "maximum_ledger_bytes", "maximum_compact_context_bytes", "output_decimal_places",
    "session_spine", "missing_session_rule", "backfill_rule", "checkpoint_rule",
    "incumbent_composite_rule", "return_provenance_rule", "append_rule",
    "summary_cache_rule", "safety",
}

_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "version": "1.0.0",
    "evidence_gate_schema": evidence_gate.RESULT_SCHEMA,
    "plan_schema": PLAN_SCHEMA,
    "return_bundle_schema": RETURN_BUNDLE_SCHEMA,
    "draft_schema": DRAFT_SCHEMA,
    "event_schema": EVENT_SCHEMA,
    "summary_schema": SUMMARY_SCHEMA,
    "required_evidence_gate_status": "eligible_for_shadow_observation_review",
    "output_root": "data/runtime/quant/strategy_shadow",
    "session_spine": "versioned_us_market_calendar_no_compression",
    "missing_session_rule": "append_explicit_unavailable_event_never_silently_omit",
    "backfill_rule": (
        "complete_returns_after_recording_deadline_forbidden_unavailable_marker_allowed"),
    "checkpoint_rule": (
        "evaluate_only_latest_reached_preregistered_prefix_never_rolling_optional_stop"),
    "incumbent_composite_rule": "equal_weight_daily_net_return",
    "return_provenance_rule": (
        "derive_from_hash_bound_standard_portfolio_nav_never_accept_caller_return"),
    "append_rule": "exclusive_lock_validate_head_append_one_fsync",
    "summary_cache_rule": (
        "same_plan_ledger_head_and_as_of_reuses_identical_output_conflicts_fail_closed"),
}

_PLAN_KEYS = set(SAFETY) | {
    "schema", "tool", "ok", "plan_id", "created_at_utc", "start_session_date",
    "policy_version", "policy_sha256", "approval_reference", "evidence_gate",
    "cohort_lock", "policy_contracts", "candidate", "benchmark", "incumbents",
    "checkpoints", "observation_contract",
    "limitations", "profitability_status", "shadow_observation_started",
    "human_approval_recorded", "actual_orders_count", "plan_sha256",
}
_STRATEGY_KEYS = {
    "strategy_id", "portfolio_id", "role", "historical_source", "strategy_contract",
}
_STRATEGY_SOURCE_KEYS = {"path", "file_sha256", "result_sha256", "content_sha256"}
_STRATEGY_CONTRACT_KEYS = {
    "schema", "variant_id", "plan_version", "cost_stress_multiplier",
    "cost_policy_version", "portfolio_policy_version", "cost_policy_sha256",
    "portfolio_policy_sha256", "conservative_rules_sha256",
    "source_backtest_plan_sha256", "portfolio_plan_sha256",
    "benchmark_semantic_validation_required", "replay_spec_complete",
    "replay_spec_sha256", "strategy_contract_sha256",
}
_SOURCE_KEYS = {"path", "file_sha256", "result_sha256", "status"}
_COHORT_LOCK_KEYS = {
    "cohort_id", "cohort_sha256", "registry_lock_head_sha256",
    "uniqueness_result_sha256", "source_set_sha256",
}
_POLICY_CONTRACT_KEYS = {"path", "file_sha256", "semantic_sha256"}
_RETURN_BUNDLE_KEYS = {
    "schema", "tool", "plan_id", "plan_sha256", "session_date", "generated_at_utc",
    "data_cutoff_utc", "candidate", "benchmark", "incumbents", "source_results",
    "derivation_contract", "net_of_modeled_costs", "contains_future_data",
    "contains_account_data", "actual_orders_count", "bundle_sha256",
}
_RETURN_SOURCE_KEYS = {
    "role", "strategy_id", "path", "file_sha256", "result_sha256",
    "daily_path_sha256", "strategy_contract_sha256", "previous_row_sha256",
    "terminal_row_sha256", "validation_level",
}
_DRAFT_KEYS = {
    "schema", "tool", "plan_id", "plan_sha256", "session_date", "state",
    "generated_at_utc", "data_cutoff_utc", "candidate", "benchmark", "incumbents",
    "return_bundle", "reason_codes", "net_of_modeled_costs", "contains_future_data",
    "contains_account_data", "actual_orders_count", "draft_sha256",
}
_OBS_KEYS = {"strategy_id", "net_return", "active", "gross_exposure"}
_DRAFT_BUNDLE_KEYS = {"path", "file_sha256", "bundle_sha256"}
_EVENT_KEYS = set(SAFETY) | {
    "schema", "tool", "plan_id", "sequence", "event_type", "recorded_at_utc",
    "previous_event_sha256", "policy_sha256", "plan_sha256", "payload", "event_sha256",
}
_ANCHOR_KEYS = {"plan_path", "plan_file_sha256", "plan_sha256"}
_OBS_EVENT_KEYS = {
    "session_date", "state", "draft_path", "draft_file_sha256", "draft_sha256",
    "candidate", "benchmark", "incumbents", "reason_codes",
}
_SUMMARY_KEYS = set(SAFETY) | {
    "schema", "tool", "ok", "generated_at_utc", "plan_id", "plan_sha256",
    "ledger_file_sha256", "ledger_head_sha256", "cache_key", "status",
    "observation_progress", "checkpoint_evaluation", "current_metrics", "quality_gates",
    "failed_gate_codes", "reliability_status", "uniqueness_status",
    "profitability_status", "eligible_for_human_shadow_evidence_review", "compact_context",
    "limitations", "actual_orders_count", "summary_sha256",
}
_PROGRESS_KEYS = {
    "start_session_date", "latest_session_date", "elapsed_sessions", "complete_sessions",
    "unavailable_sessions", "expected_closed_sessions_as_of",
    "session_denominator_complete", "missing_session_dates", "next_expected_session_date",
    "latest_reached_decision_checkpoint", "next_preregistered_checkpoint",
    "maximum_observation_sessions", "observation_horizon_complete",
}
_DESCRIPTIVE_KEYS = {
    "elapsed_sessions", "complete_sessions", "unavailable_sessions", "complete_fraction",
    "candidate_active_sessions", "candidate_active_fraction", "candidate_total_return",
    "benchmark_total_return", "candidate_excess_mean_daily_return",
    "candidate_positive_day_fraction", "candidate_max_drawdown",
}
_CHECKPOINT_KEYS = {
    "checkpoint_sessions", "two_sided_alpha", "alpha_spent_through_checkpoint",
    "prefix_metrics", "candidate_vs_benchmark_hac",
    "candidate_vs_incumbent_composite_hac", "incumbent_correlations",
    "incumbent_active_day_jaccards", "maximum_absolute_incumbent_correlation",
    "maximum_incumbent_active_day_jaccard",
}
_HAC_BASE_KEYS = {
    "available", "reason", "n", "lag", "mean", "standard_error",
    "confidence_interval", "lower_bound_above_zero",
}
_HAC_AVAILABLE_KEYS = _HAC_BASE_KEYS | {"two_sided_alpha", "critical_value"}
_GATE_KEYS = {"code", "category", "passed", "observed", "required"}
_COMPACT_KEYS = {
    "plan_id", "status", "elapsed_sessions", "complete_sessions",
    "expected_closed_sessions_as_of", "session_denominator_complete",
    "missing_session_count", "checkpoint_sessions", "next_checkpoint",
    "candidate_total_return", "candidate_max_drawdown", "failed_gate_codes",
    "profitability_status", "execution_enabled",
}


class ShadowError(RuntimeError):
    """Fail-closed contract violation."""


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


def _round(value: float | None, places: int = 10) -> float | None:
    if value is None:
        return None
    output = round(float(value), places)
    return 0.0 if output == 0 else output


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trusted_utc(repository_root: Path, test_value: str | None = None) -> str:
    """Use wall-clock UTC in production; permit deterministic time only in temp repositories."""
    if test_value is None:
        return utc(dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    parsed = parse_utc(test_value)
    if parsed is None:
        raise ShadowError("test_time_invalid")
    if repository_root.resolve() == ROOT.resolve():
        raise ShadowError("test_time_injection_forbidden_for_production_repository")
    return utc(parsed)


def parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _read_json(path: Path, maximum: int = 128 * 1024 * 1024) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ShadowError("input_not_regular_bounded_file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShadowError("input_json_invalid") from exc
    if not isinstance(value, dict):
        raise ShadowError("input_not_object")
    return value


def _safe_relative(value: Any, roots: list[str]) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return False
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def _resolve(value: Any, repository_root: Path, policy: dict) -> Path:
    if not _safe_relative(value, policy["allowed_input_roots"]):
        raise ShadowError("path_not_allowlisted_relative")
    if repository_root.is_symlink() or not repository_root.is_dir():
        raise ShadowError("repository_root_invalid")
    root = repository_root.resolve()
    current = root
    for part in Path(value).parts:
        current = current / part
        if current.is_symlink():
            raise ShadowError("path_symlink_not_allowed")
    path = root / value
    if not path.is_file():
        raise ShadowError("path_not_regular_file")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ShadowError("path_escapes_repository_root") from exc
    return path.resolve()


def _portable(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as exc:
        raise ShadowError("path_outside_repository") from exc


def _output_path(path: Path, repository_root: Path, policy: dict) -> Path:
    root = repository_root.resolve()
    output_root = (root / policy["output_root"]).resolve()
    candidate = (path if path.is_absolute() else root / path).resolve()
    parent = candidate.parent
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ShadowError("output_outside_repository") from exc
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ShadowError("output_parent_symlink_not_allowed")
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise ShadowError("output_not_under_policy_root") from exc
    if parent.exists() and parent.is_symlink():
        raise ShadowError("output_parent_symlink_not_allowed")
    return candidate


def _atomic_write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        raise ShadowError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise ShadowError("output_already_exists")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _write_json_once(path: Path, document: dict) -> None:
    _atomic_write_once(path, json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _write_idempotent(path: Path, document: dict) -> str:
    payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ShadowError("summary_output_not_regular_file")
        if path.read_bytes() != payload:
            raise ShadowError("summary_output_conflict")
        return "reused"
    _atomic_write_once(path, payload)
    return "written"


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["shadow_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("shadow_policy_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"shadow_policy_fixed_rule_invalid:{key}")
    if document.get("safety") != SAFETY:
        errors.append("shadow_policy_safety_invalid")
    roots = document.get("allowed_input_roots")
    if (not isinstance(roots, list) or not roots or len(roots) != len(set(roots))
            or any(not isinstance(root, str) or not root.startswith("data/runtime/")
                   for root in roots)):
        errors.append("shadow_policy_allowed_roots_invalid")
    descriptive = document.get("descriptive_checkpoints_sessions")
    if (not isinstance(descriptive, list) or descriptive != sorted(set(descriptive or []))
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                   for value in (descriptive or []))):
        errors.append("shadow_policy_descriptive_checkpoints_invalid")
    schedule = document.get("decision_checkpoint_alpha_schedule")
    sessions: list[int] = []
    alpha_total = 0.0
    if not isinstance(schedule, list) or not schedule:
        errors.append("shadow_policy_alpha_schedule_invalid")
    else:
        for index, row in enumerate(schedule):
            if not isinstance(row, dict) or set(row) != {"session_count", "two_sided_alpha"}:
                errors.append(f"shadow_policy_alpha_row_invalid:{index}")
                continue
            count, alpha = row.get("session_count"), row.get("two_sided_alpha")
            if not isinstance(count, int) or isinstance(count, bool) or count < 60:
                errors.append(f"shadow_policy_checkpoint_invalid:{index}")
            else:
                sessions.append(count)
            if not _number(alpha) or not 0 < float(alpha) <= 0.05:
                errors.append(f"shadow_policy_alpha_invalid:{index}")
            else:
                alpha_total += float(alpha)
    if sessions != sorted(set(sessions)):
        errors.append("shadow_policy_checkpoint_order_invalid")
    if not _number(document.get("maximum_familywise_alpha")) or alpha_total > float(
            document.get("maximum_familywise_alpha", 0)) + 1e-12:
        errors.append("shadow_policy_familywise_alpha_invalid")
    bounded = {
        "minimum_complete_fraction": (0.9, 1.0),
        "minimum_candidate_active_fraction": (0.0, 1.0),
        "maximum_absolute_incumbent_correlation": (0.0, 0.95),
        "maximum_active_day_jaccard": (0.0, 0.95),
        "maximum_shadow_drawdown": (0.0, 0.5),
        "maximum_absolute_daily_return": (0.0, 1.0),
        "maximum_gross_exposure": (0.0, 1.0),
    }
    for key, (lower, upper) in bounded.items():
        value = document.get(key)
        if not _number(value) or not lower < float(value) <= upper:
            errors.append(f"shadow_policy_number_invalid:{key}")
    for key in ("minimum_candidate_total_return",
                "minimum_benchmark_excess_hac_lower_bound",
                "minimum_incumbent_excess_hac_lower_bound"):
        if document.get(key) != 0.0:
            errors.append(f"shadow_policy_zero_threshold_invalid:{key}")
    for key, lower, upper in (
        ("minimum_hac_observations", 30, 504),
        ("maximum_observation_sessions", 252, 1008),
        ("maximum_recording_delay_hours_for_complete", 1, 72),
        ("maximum_ledger_bytes", 1024 * 1024, 1024 * 1024 * 1024),
        ("maximum_compact_context_bytes", 1024, 8192),
        ("output_decimal_places", 6, 12),
    ):
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            errors.append(f"shadow_policy_integer_invalid:{key}")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read_json(path)
    except ShadowError:
        return None, ["shadow_policy_unreadable"]
    return document, validate_policy(document)


def _calendar() -> tuple[set[str], set[str]]:
    return validate_market_day.load_calendar(validate_market_day.CONFIG)


def _session_verdict(day: dt.date) -> dict:
    holidays, early = _calendar()
    verdict = validate_market_day.verdict(day, holidays, early)
    if not verdict["calendar_covers_year"]:
        raise ShadowError("market_calendar_year_not_covered")
    return verdict


def _session_close(day: dt.date) -> dt.datetime:
    verdict = _session_verdict(day)
    if not verdict["is_trading_day"]:
        raise ShadowError("date_not_trading_session")
    hour = 13 if verdict["is_early_close"] else 16
    return dt.datetime.combine(day, dt.time(hour, 0), tzinfo=_ET).astimezone(dt.timezone.utc)


def next_session(day: dt.date) -> dt.date:
    current = day + dt.timedelta(days=1)
    for _ in range(16):
        verdict = _session_verdict(current)
        if verdict["is_trading_day"]:
            return current
        current += dt.timedelta(days=1)
    raise ShadowError("next_market_session_not_found")


def previous_session(day: dt.date) -> dt.date:
    current = day - dt.timedelta(days=1)
    for _ in range(16):
        verdict = _session_verdict(current)
        if verdict["is_trading_day"]:
            return current
        current -= dt.timedelta(days=1)
    raise ShadowError("previous_market_session_not_found")


def _closed_sessions(start: dt.date, as_of: dt.datetime, maximum: int) -> list[str]:
    sessions: list[str] = []
    current = start
    while len(sessions) < maximum and _session_close(current) <= as_of:
        sessions.append(current.isoformat())
        if len(sessions) < maximum:
            current = next_session(current)
    return sessions


def _replay_spec_payload(contract: dict) -> dict:
    keys = (
        "schema", "variant_id", "plan_version", "cost_stress_multiplier",
        "cost_policy_version", "portfolio_policy_version", "cost_policy_sha256",
        "portfolio_policy_sha256", "conservative_rules_sha256",
        "source_backtest_plan_sha256", "portfolio_plan_sha256",
        "benchmark_semantic_validation_required", "replay_spec_complete",
    )
    return {key: contract.get(key) for key in keys}


def _strategy_contract(document: dict, *, benchmark: bool) -> dict:
    lineage = document.get("lineage") if isinstance(document.get("lineage"), dict) else {}
    versions = (document.get("policy_versions")
                if isinstance(document.get("policy_versions"), dict) else {})
    conservative = document.get("conservative_rules")
    if not isinstance(conservative, list) or not conservative or any(
            not isinstance(value, str) or not value for value in conservative):
        raise ShadowError("strategy_conservative_rules_invalid")
    source_plan_key = ("source_candidate_backtest_plan_sha256" if benchmark
                       else "source_backtest_plan_sha256")
    portfolio_plan_key = ("source_candidate_portfolio_plan_sha256" if benchmark
                          else "portfolio_plan_sha256")
    values = {
        "schema": STRATEGY_CONTRACT_SCHEMA,
        "variant_id": document.get("variant_id"),
        "plan_version": versions.get("plan"),
        "cost_stress_multiplier": document.get("cost_stress_multiplier"),
        "cost_policy_version": versions.get("cost"),
        "portfolio_policy_version": versions.get("portfolio"),
        "cost_policy_sha256": lineage.get("cost_policy_sha256"),
        "portfolio_policy_sha256": lineage.get("portfolio_policy_sha256"),
        "conservative_rules_sha256": sha256_obj(conservative),
        "source_backtest_plan_sha256": lineage.get(source_plan_key),
        "portfolio_plan_sha256": lineage.get(portfolio_plan_key),
        "benchmark_semantic_validation_required": benchmark,
        "replay_spec_complete": True,
        "replay_spec_sha256": "",
        "strategy_contract_sha256": "",
    }
    if (not isinstance(values["variant_id"], str) or not values["variant_id"]
            or not isinstance(values["plan_version"], str) or not values["plan_version"]
            or not _number(values["cost_stress_multiplier"])
            or float(values["cost_stress_multiplier"]) <= 0
            or not isinstance(values["cost_policy_version"], str)
            or not isinstance(values["portfolio_policy_version"], str)
            or any(not isinstance(values[key], str) or not _SHA_RE.fullmatch(values[key])
                   for key in ("cost_policy_sha256", "portfolio_policy_sha256",
                               "source_backtest_plan_sha256",
                               "portfolio_plan_sha256"))):
        raise ShadowError("strategy_contract_fields_invalid")
    values["cost_stress_multiplier"] = float(values["cost_stress_multiplier"])
    values["replay_spec_sha256"] = sha256_obj(_replay_spec_payload(values))
    values["strategy_contract_sha256"] = sha256_obj(
        _payload(values, "strategy_contract_sha256"))
    return values


def _policy_contracts() -> dict[str, dict]:
    specs = {
        "inference_policy": (inference_audit.POLICY_DEFAULT, inference_audit.load_policy,
                             inference_audit.sha256_obj),
        "uniqueness_policy": (uniqueness.POLICY_DEFAULT, uniqueness.load_policy,
                              uniqueness.sha256_obj),
        "artifact_policy": (strategy_artifacts.POLICY_DEFAULT,
                            strategy_artifacts.load_policy, strategy_artifacts.sha256_obj),
    }
    refs: dict[str, dict] = {}
    for role, (path, loader, hasher) in specs.items():
        document, errors = loader(path)
        if document is None or errors:
            raise ShadowError(f"policy_contract_invalid:{role}")
        refs[role] = {
            "path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
            "file_sha256": sha256_file(path),
            "semantic_sha256": hasher(document),
        }
    calendar_path = validate_market_day.CONFIG
    refs["market_calendar"] = {
        "path": calendar_path.resolve().relative_to(ROOT.resolve()).as_posix(),
        "file_sha256": sha256_file(calendar_path),
        "semantic_sha256": sha256_file(calendar_path),
    }
    return refs


def _read_bound_reference(ref: dict, repository_root: Path, policy: dict,
                          semantic_key: str) -> tuple[Path, dict]:
    path = _resolve(ref.get("path"), repository_root, policy)
    document = _read_json(path)
    if sha256_file(path) != ref.get("file_sha256"):
        raise ShadowError("bound_reference_file_hash_mismatch")
    if document.get(semantic_key) != ref.get("semantic_sha256"):
        raise ShadowError("bound_reference_semantic_hash_mismatch")
    return path, document


def _strategy_set(gate_document: dict, repository_root: Path,
                  policy: dict) -> tuple[dict, dict, list[dict], dict]:
    refs = [row for row in gate_document.get("input_artifacts", [])
            if isinstance(row, dict) and row.get("role") == "cohort_audit"]
    if len(refs) != 1:
        raise ShadowError("cohort_audit_reference_count_invalid")
    _path, wrapper = _read_bound_reference(
        refs[0], repository_root, policy, "audit_sha256")
    wrapper_errors = cohort.validate_audit_wrapper(wrapper)
    if wrapper_errors:
        raise ShadowError("cohort_audit_invalid:" + wrapper_errors[0])
    result = wrapper.get("uniqueness_result")
    if not isinstance(result, dict) or uniqueness.validate_result(result):
        raise ShadowError("nested_uniqueness_result_invalid")
    sources = result.get("source_artifacts")
    if not isinstance(sources, dict):
        raise ShadowError("uniqueness_sources_invalid")
    candidate = sources.get("candidate")
    benchmark = sources.get("benchmark")
    incumbents = sources.get("incumbents")
    if (not isinstance(candidate, dict) or not isinstance(benchmark, dict)
            or not isinstance(incumbents, list) or not incumbents):
        raise ShadowError("uniqueness_strategy_set_invalid")

    artifact_policy, artifact_errors = strategy_artifacts.load_policy()
    if artifact_policy is None or artifact_errors:
        raise ShadowError("artifact_policy_invalid")
    gate_refs = gate_document.get("input_artifacts")
    if not isinstance(gate_refs, list):
        raise ShadowError("evidence_gate_input_artifacts_invalid")

    def compact(source: dict, role: str) -> dict:
        result_sha = source.get("result_sha256")
        candidates = [
            ref for ref in gate_refs if isinstance(ref, dict)
            and ref.get("semantic_sha256") == result_sha
            and ref.get("schema") == portfolio_backtest.RESULT_SCHEMA
        ]
        preferred = {
            "candidate": "candidate_portfolio",
            "benchmark": "benchmark_portfolio",
            "incumbent": "spa_model_source:",
        }[role]
        preferred_refs = [ref for ref in candidates if (
            ref.get("role") == preferred if role != "incumbent"
            else str(ref.get("role", "")).startswith(preferred))]
        selected = sorted(preferred_refs or candidates,
                          key=lambda ref: (str(ref.get("role")), str(ref.get("path"))))
        if not selected:
            raise ShadowError(f"strategy_source_reference_missing:{role}")
        ref = selected[0]
        path = _resolve(ref.get("path"), repository_root, policy)
        document = _read_json(path)
        validation = portfolio_backtest.validate_result(document)
        if validation:
            raise ShadowError(f"strategy_source_invalid:{role}:{validation[0]}")
        if role == "benchmark":
            benchmark_errors = strategy_artifacts.validate_benchmark(
                document, artifact_policy)
            if benchmark_errors:
                raise ShadowError("benchmark_source_not_semantically_rebuildable:"
                                  + benchmark_errors[0])
        content_sha = sha256_obj(document)
        lineage = document.get("lineage", {})
        versions = document.get("policy_versions", {})
        expected = {
            "variant_id": source.get("variant_id"),
            "result_sha256": result_sha,
            "content_sha256": source.get("content_sha256"),
            "cost_stress_multiplier": source.get("cost_stress_multiplier"),
            "cost_policy_version": source.get("cost_policy_version"),
            "portfolio_policy_version": source.get("portfolio_policy_version"),
            "cost_policy_sha256": source.get("cost_policy_sha256"),
            "portfolio_policy_sha256": source.get("portfolio_policy_sha256"),
        }
        observed = {
            "variant_id": document.get("variant_id"),
            "result_sha256": document.get("result_sha256"),
            "content_sha256": content_sha,
            "cost_stress_multiplier": float(document.get("cost_stress_multiplier"))
            if _number(document.get("cost_stress_multiplier")) else None,
            "cost_policy_version": versions.get("cost"),
            "portfolio_policy_version": versions.get("portfolio"),
            "cost_policy_sha256": lineage.get("cost_policy_sha256"),
            "portfolio_policy_sha256": lineage.get("portfolio_policy_sha256"),
        }
        if observed != expected or sha256_file(path) != ref.get("file_sha256"):
            raise ShadowError(f"strategy_source_binding_mismatch:{role}")
        return {
            "strategy_id": source.get("strategy_id"),
            "portfolio_id": source.get("variant_id"),
            "role": role,
            "historical_source": {
                "path": ref["path"],
                "file_sha256": ref["file_sha256"],
                "result_sha256": result_sha,
                "content_sha256": content_sha,
            },
            "strategy_contract": _strategy_contract(
                document, benchmark=role == "benchmark"),
        }

    compact_candidate = compact(candidate, "candidate")
    compact_benchmark = compact(benchmark, "benchmark")
    compact_incumbents = sorted((compact(row, "incumbent") for row in incumbents),
                                key=lambda row: str(row["strategy_id"]))
    expected = {
        "candidate_strategy_id": compact_candidate["strategy_id"],
        "candidate_portfolio_id": compact_candidate["portfolio_id"],
        "benchmark_strategy_id": compact_benchmark["strategy_id"],
        "benchmark_portfolio_id": compact_benchmark["portfolio_id"],
    }
    if any(gate_document.get(key) != value for key, value in expected.items()):
        raise ShadowError("evidence_gate_strategy_identity_mismatch")
    bindings = gate_document.get("bindings", {})
    if (bindings.get("candidate_result_sha256") != compact_candidate[
            "historical_source"]["result_sha256"]
            or bindings.get("benchmark_result_sha256") != compact_benchmark[
                "historical_source"]["result_sha256"]):
        raise ShadowError("evidence_gate_strategy_hash_mismatch")
    cohort_lock = {
        "cohort_id": wrapper.get("cohort_id"),
        "cohort_sha256": wrapper.get("cohort_sha256"),
        "registry_lock_head_sha256": wrapper.get("registry_lock_head_sha256"),
        "uniqueness_result_sha256": wrapper.get("uniqueness_result_sha256"),
        "source_set_sha256": result.get("source_set_sha256"),
    }
    if (set(cohort_lock) != _COHORT_LOCK_KEYS
            or not isinstance(cohort_lock["cohort_id"], str)
            or any(not isinstance(cohort_lock[key], str)
                   or not _SHA_RE.fullmatch(cohort_lock[key])
                   for key in _COHORT_LOCK_KEYS - {"cohort_id"})):
        raise ShadowError("cohort_lock_invalid")
    return compact_candidate, compact_benchmark, compact_incumbents, cohort_lock


def build_plan(*, evidence_gate_path: Path, start_session_date: str,
               approval_reference: str, created_at_utc: str | None = None,
               repository_root: Path = ROOT,
               policy_path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(policy_path)
    if policy is None or errors:
        raise ShadowError("shadow_policy_invalid")
    created_at_utc = _trusted_utc(repository_root, created_at_utc)
    created = parse_utc(created_at_utc)
    start = parse_date(start_session_date)
    if created is None:
        raise ShadowError("created_at_utc_invalid")
    if start is None:
        raise ShadowError("start_session_date_invalid")
    if not isinstance(approval_reference, str) or not _TOKEN_RE.fullmatch(approval_reference):
        raise ShadowError("approval_reference_invalid")
    verdict = _session_verdict(start)
    if not verdict["is_trading_day"]:
        raise ShadowError("start_date_not_trading_session")
    gate_portable = _portable(evidence_gate_path, repository_root)
    gate_path = _resolve(gate_portable, repository_root, policy)
    gate_document = _read_json(gate_path)
    gate_errors = evidence_gate.validate_result(gate_document)
    if gate_errors:
        raise ShadowError("evidence_gate_invalid:" + gate_errors[0])
    if gate_document.get("status") != policy["required_evidence_gate_status"]:
        raise ShadowError("evidence_gate_not_eligible")
    target = parse_date(gate_document.get("target_session_date"))
    generated = evidence_gate._parse_utc(gate_document.get("generated_utc"))
    if target is None or generated is None:
        raise ShadowError("evidence_gate_time_invalid")
    created_pt_date = created.astimezone(_PT).date()
    if start <= max(target, created_pt_date) or created < generated:
        raise ShadowError("retroactive_or_pre_evidence_shadow_plan_forbidden")
    candidate, benchmark, incumbents, cohort_lock = _strategy_set(
        gate_document, repository_root, policy)
    strategy_rows = [candidate, benchmark, *incumbents]
    if any(set(row) != _STRATEGY_KEYS or not _ID_RE.fullmatch(str(row["strategy_id"]))
           or not isinstance(row["portfolio_id"], str) or not row["portfolio_id"]
           for row in strategy_rows):
        raise ShadowError("strategy_binding_invalid")
    if len({row["strategy_id"] for row in strategy_rows}) != len(strategy_rows):
        raise ShadowError("strategy_id_duplicate")
    plan_id = f"shadow:{candidate['strategy_id']}:{gate_document['result_sha256'][:16]}"
    document = {
        **SAFETY,
        "schema": PLAN_SCHEMA,
        "tool": TOOL,
        "ok": True,
        "plan_id": plan_id,
        "created_at_utc": created_at_utc,
        "start_session_date": start_session_date,
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "approval_reference": approval_reference,
        "evidence_gate": {
            "path": gate_portable,
            "file_sha256": sha256_file(gate_path),
            "result_sha256": gate_document["result_sha256"],
            "status": gate_document["status"],
        },
        "cohort_lock": cohort_lock,
        "policy_contracts": _policy_contracts(),
        "candidate": candidate,
        "benchmark": benchmark,
        "incumbents": incumbents,
        "checkpoints": {
            "descriptive_sessions": policy["descriptive_checkpoints_sessions"],
            "decision_alpha_schedule": policy["decision_checkpoint_alpha_schedule"],
            "maximum_familywise_alpha": policy["maximum_familywise_alpha"],
            "evaluation_rule": policy["checkpoint_rule"],
        },
        "observation_contract": {
            "session_spine": policy["session_spine"],
            "missing_session_rule": policy["missing_session_rule"],
            "backfill_rule": policy["backfill_rule"],
            "net_of_modeled_costs_required": True,
            "return_provenance_rule": policy["return_provenance_rule"],
            "standard_return_schema": portfolio_backtest.RESULT_SCHEMA,
            "return_bundle_schema": RETURN_BUNDLE_SCHEMA,
            "frozen_incumbent_composite_rule": policy["incumbent_composite_rule"],
            "maximum_sessions": policy["maximum_observation_sessions"],
        },
        "limitations": LIMITATIONS,
        "profitability_status": "not_established",
        "shadow_observation_started": False,
        "human_approval_recorded": True,
        "actual_orders_count": 0,
        "plan_sha256": "",
    }
    document["plan_sha256"] = sha256_obj(_payload(document, "plan_sha256"))
    validation = validate_plan(document, repository_root=repository_root,
                               policy_path=policy_path)
    if validation:
        raise ShadowError("plan_self_validation:" + validation[0])
    return document


def validate_plan(document: Any, *, repository_root: Path = ROOT,
                  policy_path: Path = POLICY_DEFAULT,
                  revalidate_source: bool = True) -> list[str]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return ["plan_policy_invalid"]
    if not isinstance(document, dict):
        return ["plan_not_object"]
    errors: list[str] = []
    if set(document) != _PLAN_KEYS:
        errors.append("plan_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"plan_safety_invalid:{key}")
    if (document.get("schema") != PLAN_SCHEMA or document.get("tool") != TOOL
            or document.get("ok") is not True):
        errors.append("plan_identity_invalid")
    if not isinstance(document.get("plan_id"), str) or not _ID_RE.fullmatch(
            document.get("plan_id", "")):
        errors.append("plan_id_invalid")
    created = parse_utc(document.get("created_at_utc"))
    start = parse_date(document.get("start_session_date"))
    if created is None:
        errors.append("plan_created_at_invalid")
    if start is None:
        errors.append("plan_start_date_invalid")
    else:
        try:
            if not _session_verdict(start)["is_trading_day"]:
                errors.append("plan_start_not_trading_session")
        except ShadowError:
            errors.append("plan_start_calendar_invalid")
    if document.get("policy_version") != policy["version"]:
        errors.append("plan_policy_version_invalid")
    if document.get("policy_sha256") != sha256_obj(policy):
        errors.append("plan_policy_hash_invalid")
    if not isinstance(document.get("approval_reference"), str) or not _TOKEN_RE.fullmatch(
            document.get("approval_reference", "")):
        errors.append("plan_approval_reference_invalid")
    source = document.get("evidence_gate")
    if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
        errors.append("plan_evidence_source_invalid")
    cohort_lock = document.get("cohort_lock")
    if (not isinstance(cohort_lock, dict) or set(cohort_lock) != _COHORT_LOCK_KEYS
            or not isinstance(cohort_lock.get("cohort_id"), str)
            or any(not isinstance(cohort_lock.get(key), str)
                   or not _SHA_RE.fullmatch(cohort_lock.get(key, ""))
                   for key in _COHORT_LOCK_KEYS - {"cohort_id"})):
        errors.append("plan_cohort_lock_invalid")
    try:
        expected_policy_contracts = _policy_contracts()
    except ShadowError:
        expected_policy_contracts = None
    policy_contracts = document.get("policy_contracts")
    if (not isinstance(policy_contracts, dict)
            or set(policy_contracts) != {"inference_policy", "uniqueness_policy",
                                         "artifact_policy", "market_calendar"}
            or any(not isinstance(ref, dict) or set(ref) != _POLICY_CONTRACT_KEYS
                   for ref in policy_contracts.values())
            or policy_contracts != expected_policy_contracts):
        errors.append("plan_policy_contracts_invalid")
    rows = [document.get("candidate"), document.get("benchmark")]
    incumbents = document.get("incumbents")
    if not isinstance(incumbents, list) or not incumbents:
        errors.append("plan_incumbents_invalid")
        incumbents = []
    rows.extend(incumbents)
    ids: list[str] = []
    expected_roles = ["candidate", "benchmark", *(["incumbent"] * len(incumbents))]
    for index, (row, expected_role) in enumerate(zip(rows, expected_roles)):
        if not isinstance(row, dict) or set(row) != _STRATEGY_KEYS:
            errors.append(f"plan_strategy_invalid:{index}")
            continue
        ids.append(row.get("strategy_id"))
        historical = row.get("historical_source")
        contract = row.get("strategy_contract")
        if (not isinstance(row.get("strategy_id"), str)
                or not _ID_RE.fullmatch(row.get("strategy_id", ""))
                or not isinstance(row.get("portfolio_id"), str) or not row["portfolio_id"]
                or row.get("role") != expected_role
                or not isinstance(historical, dict)
                or set(historical) != _STRATEGY_SOURCE_KEYS
                or not _safe_relative(historical.get("path"), policy["allowed_input_roots"])
                or any(not isinstance(historical.get(key), str)
                       or not _SHA_RE.fullmatch(historical.get(key, ""))
                       for key in _STRATEGY_SOURCE_KEYS - {"path"})
                or not isinstance(contract, dict)
                or set(contract) != _STRATEGY_CONTRACT_KEYS
                or contract.get("schema") != STRATEGY_CONTRACT_SCHEMA
                or contract.get("variant_id") != row.get("portfolio_id")
                or not isinstance(contract.get("plan_version"), str)
                or not contract.get("plan_version")
                or not _number(contract.get("cost_stress_multiplier"))
                or float(contract.get("cost_stress_multiplier", 0)) <= 0
                or not isinstance(contract.get("cost_policy_version"), str)
                or not contract.get("cost_policy_version")
                or not isinstance(contract.get("portfolio_policy_version"), str)
                or not contract.get("portfolio_policy_version")
                or any(not isinstance(contract.get(key), str)
                       or not _SHA_RE.fullmatch(contract.get(key, ""))
                       for key in ("cost_policy_sha256", "portfolio_policy_sha256",
                                   "conservative_rules_sha256",
                                   "source_backtest_plan_sha256",
                                   "portfolio_plan_sha256", "replay_spec_sha256"))
                or contract.get("benchmark_semantic_validation_required") is not (
                    expected_role == "benchmark")
                or contract.get("replay_spec_complete") is not True
                or contract.get("replay_spec_sha256") != sha256_obj(
                    _replay_spec_payload(contract))
                or contract.get("strategy_contract_sha256") != sha256_obj(
                    _payload(contract, "strategy_contract_sha256"))):
            errors.append(f"plan_strategy_fields_invalid:{index}")
    if ids != [value for value in ids if isinstance(value, str)] or len(ids) != len(set(ids)):
        errors.append("plan_strategy_ids_invalid")
    if [row.get("strategy_id") for row in incumbents if isinstance(row, dict)] != sorted(
            row.get("strategy_id") for row in incumbents if isinstance(row, dict)):
        errors.append("plan_incumbent_order_invalid")
    expected_checkpoints = {
        "descriptive_sessions": policy["descriptive_checkpoints_sessions"],
        "decision_alpha_schedule": policy["decision_checkpoint_alpha_schedule"],
        "maximum_familywise_alpha": policy["maximum_familywise_alpha"],
        "evaluation_rule": policy["checkpoint_rule"],
    }
    if document.get("checkpoints") != expected_checkpoints:
        errors.append("plan_checkpoints_invalid")
    expected_contract = {
        "session_spine": policy["session_spine"],
        "missing_session_rule": policy["missing_session_rule"],
        "backfill_rule": policy["backfill_rule"],
        "net_of_modeled_costs_required": True,
        "return_provenance_rule": policy["return_provenance_rule"],
        "standard_return_schema": portfolio_backtest.RESULT_SCHEMA,
        "return_bundle_schema": RETURN_BUNDLE_SCHEMA,
        "frozen_incumbent_composite_rule": policy["incumbent_composite_rule"],
        "maximum_sessions": policy["maximum_observation_sessions"],
    }
    if document.get("observation_contract") != expected_contract:
        errors.append("plan_observation_contract_invalid")
    if (document.get("limitations") != LIMITATIONS
            or document.get("profitability_status") != "not_established"
            or document.get("shadow_observation_started") is not False
            or document.get("human_approval_recorded") is not True
            or document.get("actual_orders_count") != 0):
        errors.append("plan_non_authority_contract_invalid")
    if document.get("plan_sha256") != sha256_obj(_payload(document, "plan_sha256")):
        errors.append("plan_hash_mismatch")
    if revalidate_source and isinstance(source, dict) and set(source) == _SOURCE_KEYS:
        try:
            path = _resolve(source["path"], repository_root, policy)
            gate_document = _read_json(path)
            gate_errors = evidence_gate.validate_result(gate_document)
            if gate_errors:
                raise ShadowError("gate_invalid")
            if (sha256_file(path) != source["file_sha256"]
                    or gate_document.get("result_sha256") != source["result_sha256"]
                    or gate_document.get("status") != source["status"]
                    or source["status"] != policy["required_evidence_gate_status"]):
                raise ShadowError("gate_binding_mismatch")
            expected_candidate, expected_benchmark, expected_incumbents, expected_lock = _strategy_set(
                gate_document, repository_root, policy)
            if (document.get("candidate") != expected_candidate
                    or document.get("benchmark") != expected_benchmark
                    or incumbents != expected_incumbents
                    or cohort_lock != expected_lock):
                raise ShadowError("strategy_set_not_rederived")
            target = parse_date(gate_document.get("target_session_date"))
            generated = evidence_gate._parse_utc(gate_document.get("generated_utc"))
            if (created is None or start is None or target is None or generated is None
                    or created < generated or start <= max(target, created.astimezone(_PT).date())):
                raise ShadowError("retroactive_plan")
        except (KeyError, ShadowError):
            errors.append("plan_evidence_revalidation_failed")
    return sorted(set(errors))


def _observation(strategy: dict, net_return: float | None,
                 active: bool | None, gross_exposure: float | None) -> dict:
    return {
        "strategy_id": strategy["strategy_id"],
        "net_return": net_return,
        "active": active,
        "gross_exposure": gross_exposure,
    }


_DERIVATION_CONTRACT = {
    "source_schema": portfolio_backtest.RESULT_SCHEMA,
    "terminal_session_must_match": True,
    "previous_market_session_must_be_present": True,
    "terminal_return_rederived_from_normalized_nav": True,
    "terminal_daily_return_recurrence_must_match": True,
    "strategy_contract_must_match_plan": True,
    "benchmark_semantic_rebuild_required": True,
    "caller_supplied_returns_allowed": False,
}


def _derive_return_source(*, strategy: dict, source_path: Path, session: dt.date,
                          repository_root: Path, policy: dict,
                          artifact_policy: dict) -> tuple[dict, dict]:
    candidate_path = source_path if source_path.is_absolute() else repository_root / source_path
    portable = _portable(candidate_path, repository_root)
    resolved = _resolve(portable, repository_root, policy)
    document = _read_json(resolved)
    source_errors = portfolio_backtest.validate_result(document)
    if source_errors:
        raise ShadowError("return_source_invalid:" + source_errors[0])
    benchmark_required = strategy["strategy_contract"][
        "benchmark_semantic_validation_required"]
    if benchmark_required:
        benchmark_errors = strategy_artifacts.validate_benchmark(document, artifact_policy)
        if benchmark_errors:
            raise ShadowError("return_benchmark_semantic_rebuild_failed:"
                              + benchmark_errors[0])
    observed_contract = _strategy_contract(document, benchmark=benchmark_required)
    if observed_contract != strategy["strategy_contract"]:
        raise ShadowError("return_strategy_contract_mismatch")
    daily = document.get("daily_path")
    if not isinstance(daily, list) or len(daily) < 2:
        raise ShadowError("return_source_daily_path_too_short")
    previous, terminal = daily[-2], daily[-1]
    if (terminal.get("date") != session.isoformat()
            or previous.get("date") != previous_session(session).isoformat()):
        raise ShadowError("return_source_session_denominator_mismatch")
    previous_nav, terminal_nav = previous.get("normalized_nav"), terminal.get("normalized_nav")
    daily_return, exposure = terminal.get("daily_return"), terminal.get("gross_exposure")
    if (not _number(previous_nav) or float(previous_nav) <= 0
            or not _number(terminal_nav) or float(terminal_nav) <= 0
            or not _number(daily_return) or not _number(exposure)):
        raise ShadowError("return_source_terminal_numbers_invalid")
    derived_return = float(terminal_nav) / float(previous_nav) - 1.0
    if (abs(derived_return) > policy["maximum_absolute_daily_return"]
            or abs(float(daily_return) - derived_return) > 1e-8):
        raise ShadowError("return_source_nav_recurrence_mismatch")
    if not 0 <= float(exposure) <= policy["maximum_gross_exposure"]:
        raise ShadowError("return_source_exposure_invalid")
    observation = _observation(
        strategy, _round(derived_return, policy["output_decimal_places"]),
        float(exposure) > 1e-12, _round(float(exposure), policy["output_decimal_places"]))
    source = {
        "role": strategy["role"],
        "strategy_id": strategy["strategy_id"],
        "path": portable,
        "file_sha256": sha256_file(resolved),
        "result_sha256": document["result_sha256"],
        "daily_path_sha256": document["daily_path_sha256"],
        "strategy_contract_sha256": observed_contract["strategy_contract_sha256"],
        "previous_row_sha256": sha256_obj(previous),
        "terminal_row_sha256": sha256_obj(terminal),
        "validation_level": ("strategy_artifacts_semantic_rebuild"
                             if benchmark_required
                             else "portfolio_nav_path_semantic_rederivation"),
    }
    return observation, source


def build_return_bundle(*, plan: dict, session_date: str,
                        source_paths: dict[str, Path],
                        generated_at_utc: str | None = None,
                        data_cutoff_utc: str | None = None,
                        repository_root: Path = ROOT,
                        policy_path: Path = POLICY_DEFAULT) -> dict:
    if validate_plan(plan, repository_root=repository_root, policy_path=policy_path,
                     revalidate_source=False):
        raise ShadowError("return_bundle_plan_invalid")
    policy, errors = load_policy(policy_path)
    artifact_policy, artifact_errors = strategy_artifacts.load_policy()
    if policy is None or errors or artifact_policy is None or artifact_errors:
        raise ShadowError("return_bundle_policy_invalid")
    session = parse_date(session_date)
    if session is None or not _session_verdict(session)["is_trading_day"]:
        raise ShadowError("return_bundle_session_invalid")
    generated_at_utc = _trusted_utc(repository_root, generated_at_utc)
    generated = parse_utc(generated_at_utc)
    data_cutoff_utc = data_cutoff_utc or generated_at_utc
    cutoff = parse_utc(data_cutoff_utc)
    if (generated is None or cutoff is None or generated < cutoff
            or cutoff < _session_close(session)):
        raise ShadowError("return_bundle_timestamps_invalid")
    strategies = [plan["candidate"], plan["benchmark"], *plan["incumbents"]]
    expected_ids = {row["strategy_id"] for row in strategies}
    if set(source_paths) != expected_ids:
        raise ShadowError("return_bundle_source_denominator_mismatch")
    observations: dict[str, dict] = {}
    sources: list[dict] = []
    for strategy in strategies:
        observation, source = _derive_return_source(
            strategy=strategy, source_path=Path(source_paths[strategy["strategy_id"]]),
            session=session, repository_root=repository_root, policy=policy,
            artifact_policy=artifact_policy)
        observations[strategy["strategy_id"]] = observation
        sources.append(source)
    incumbent_rows = [observations[row["strategy_id"]] for row in plan["incumbents"]]
    document = {
        "schema": RETURN_BUNDLE_SCHEMA,
        "tool": TOOL,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "session_date": session_date,
        "generated_at_utc": generated_at_utc,
        "data_cutoff_utc": data_cutoff_utc,
        "candidate": observations[plan["candidate"]["strategy_id"]],
        "benchmark": observations[plan["benchmark"]["strategy_id"]],
        "incumbents": incumbent_rows,
        "source_results": sources,
        "derivation_contract": _DERIVATION_CONTRACT,
        "net_of_modeled_costs": True,
        "contains_future_data": False,
        "contains_account_data": False,
        "actual_orders_count": 0,
        "bundle_sha256": "",
    }
    document["bundle_sha256"] = sha256_obj(_payload(document, "bundle_sha256"))
    validation = validate_return_bundle(
        document, plan=plan, repository_root=repository_root, policy_path=policy_path,
        revalidate_sources=False)
    if validation:
        raise ShadowError("return_bundle_self_validation:" + validation[0])
    return document


def validate_return_bundle(document: Any, *, plan: dict, repository_root: Path = ROOT,
                           policy_path: Path = POLICY_DEFAULT,
                           revalidate_sources: bool = True) -> list[str]:
    policy, policy_errors = load_policy(policy_path)
    artifact_policy, artifact_errors = strategy_artifacts.load_policy()
    if policy is None or policy_errors or artifact_policy is None or artifact_errors:
        return ["return_bundle_policy_invalid"]
    if not isinstance(document, dict):
        return ["return_bundle_not_object"]
    errors: list[str] = []
    if set(document) != _RETURN_BUNDLE_KEYS:
        errors.append("return_bundle_keys_invalid")
    if (document.get("schema") != RETURN_BUNDLE_SCHEMA or document.get("tool") != TOOL
            or document.get("plan_id") != plan.get("plan_id")
            or document.get("plan_sha256") != plan.get("plan_sha256")):
        errors.append("return_bundle_identity_invalid")
    session = parse_date(document.get("session_date"))
    generated = parse_utc(document.get("generated_at_utc"))
    cutoff = parse_utc(document.get("data_cutoff_utc"))
    if session is None:
        errors.append("return_bundle_session_invalid")
    else:
        try:
            if generated is None or cutoff is None or generated < cutoff or cutoff < _session_close(
                    session):
                errors.append("return_bundle_timestamps_invalid")
        except ShadowError:
            errors.append("return_bundle_calendar_invalid")
    if document.get("derivation_contract") != _DERIVATION_CONTRACT:
        errors.append("return_bundle_derivation_contract_invalid")
    strategies = [plan.get("candidate"), plan.get("benchmark"), *plan.get("incumbents", [])]
    sources = document.get("source_results")
    if not isinstance(sources, list) or len(sources) != len(strategies):
        errors.append("return_bundle_source_denominator_invalid")
        sources = []
    expected_observations: list[dict] = []
    expected_sources: list[dict] = []
    if len(sources) == len(strategies):
        for index, (strategy, source) in enumerate(zip(strategies, sources)):
            if not isinstance(source, dict) or set(source) != _RETURN_SOURCE_KEYS:
                errors.append(f"return_bundle_source_shape_invalid:{index}")
                continue
            if (source.get("role") != strategy.get("role")
                    or source.get("strategy_id") != strategy.get("strategy_id")
                    or source.get("strategy_contract_sha256") != strategy.get(
                        "strategy_contract", {}).get("strategy_contract_sha256")):
                errors.append(f"return_bundle_source_identity_mismatch:{index}")
            try:
                path = _resolve(source["path"], repository_root, policy)
                source_document = _read_json(path)
                if (sha256_file(path) != source.get("file_sha256")
                        or source_document.get("result_sha256") != source.get("result_sha256")
                        or source_document.get("daily_path_sha256") != source.get(
                            "daily_path_sha256")):
                    errors.append(f"return_bundle_source_hash_mismatch:{index}")
            except (KeyError, ShadowError):
                errors.append(f"return_bundle_source_unavailable:{index}")
            if not revalidate_sources or session is None:
                continue
            try:
                observation, expected_source = _derive_return_source(
                    strategy=strategy, source_path=Path(source["path"]), session=session,
                    repository_root=repository_root, policy=policy,
                    artifact_policy=artifact_policy)
                expected_observations.append(observation)
                expected_sources.append(expected_source)
                if source != expected_source:
                    errors.append(f"return_bundle_source_binding_mismatch:{index}")
            except (KeyError, ShadowError):
                errors.append(f"return_bundle_source_rederivation_failed:{index}")
    if len(expected_observations) == len(strategies):
        if document.get("candidate") != expected_observations[0]:
            errors.append("return_bundle_candidate_rederivation_mismatch")
        if document.get("benchmark") != expected_observations[1]:
            errors.append("return_bundle_benchmark_rederivation_mismatch")
        if document.get("incumbents") != expected_observations[2:]:
            errors.append("return_bundle_incumbent_rederivation_mismatch")
        if sources != expected_sources:
            errors.append("return_bundle_sources_rederivation_mismatch")
    for key in ("candidate", "benchmark"):
        if not isinstance(document.get(key), dict) or set(document[key]) != _OBS_KEYS:
            errors.append(f"return_bundle_observation_invalid:{key}")
    incumbents = document.get("incumbents")
    if not isinstance(incumbents, list) or any(
            not isinstance(row, dict) or set(row) != _OBS_KEYS for row in incumbents):
        errors.append("return_bundle_incumbents_invalid")
        incumbents = []
    observation_rows = [document.get("candidate"), document.get("benchmark"), *incumbents]
    expected_ids = [strategy.get("strategy_id") for strategy in strategies]
    observed_ids: list[Any] = []
    for index, row in enumerate(observation_rows):
        if not isinstance(row, dict) or set(row) != _OBS_KEYS:
            continue
        observed_ids.append(row.get("strategy_id"))
        if (not _number(row.get("net_return"))
                or abs(float(row["net_return"])) > policy["maximum_absolute_daily_return"]
                or type(row.get("active")) is not bool
                or not _number(row.get("gross_exposure"))
                or not 0 <= float(row["gross_exposure"]) <= policy["maximum_gross_exposure"]
                or row["active"] is not (float(row["gross_exposure"]) > 1e-12)):
            errors.append(f"return_bundle_observation_value_invalid:{index}")
    if observed_ids != expected_ids:
        errors.append("return_bundle_observation_denominator_mismatch")
    if (document.get("net_of_modeled_costs") is not True
            or document.get("contains_future_data") is not False
            or document.get("contains_account_data") is not False
            or document.get("actual_orders_count") != 0):
        errors.append("return_bundle_non_authority_contract_invalid")
    if document.get("bundle_sha256") != sha256_obj(_payload(document, "bundle_sha256")):
        errors.append("return_bundle_hash_mismatch")
    return sorted(set(errors))


def build_draft(*, plan: dict, session_date: str, state: str,
                return_bundle_path: Path | None = None,
                generated_at_utc: str | None = None,
                reason_codes: list[str] | None = None,
                repository_root: Path = ROOT,
                policy_path: Path = POLICY_DEFAULT) -> dict:
    if validate_plan(plan, repository_root=repository_root, policy_path=policy_path,
                     revalidate_source=False):
        raise ShadowError("draft_plan_invalid")
    policy, errors = load_policy(policy_path)
    if policy is None or errors:
        raise ShadowError("shadow_policy_invalid")
    if state == "complete":
        if return_bundle_path is None:
            raise ShadowError("draft_complete_return_bundle_missing")
        portable = _portable(return_bundle_path, repository_root)
        resolved = _resolve(portable, repository_root, policy)
        bundle = _read_json(resolved)
        bundle_errors = validate_return_bundle(
            bundle, plan=plan, repository_root=repository_root, policy_path=policy_path)
        if bundle_errors:
            raise ShadowError("draft_return_bundle_invalid:" + bundle_errors[0])
        if bundle.get("session_date") != session_date:
            raise ShadowError("draft_return_bundle_session_mismatch")
        generated_at_utc = bundle["generated_at_utc"]
        data_cutoff_utc = bundle["data_cutoff_utc"]
        candidate_row = bundle["candidate"]
        benchmark_row = bundle["benchmark"]
        incumbent_rows = bundle["incumbents"]
        bundle_ref: dict | None = {
            "path": portable,
            "file_sha256": sha256_file(resolved),
            "bundle_sha256": bundle["bundle_sha256"],
        }
    else:
        if return_bundle_path is not None:
            raise ShadowError("draft_unavailable_return_bundle_forbidden")
        generated_at_utc = _trusted_utc(repository_root, generated_at_utc)
        data_cutoff_utc = generated_at_utc
        candidate_row = _observation(plan["candidate"], None, None, None)
        benchmark_row = _observation(plan["benchmark"], None, None, None)
        incumbent_rows = [_observation(row, None, None, None) for row in plan["incumbents"]]
        bundle_ref = None
    document = {
        "schema": DRAFT_SCHEMA,
        "tool": TOOL,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "session_date": session_date,
        "state": state,
        "generated_at_utc": generated_at_utc,
        "data_cutoff_utc": data_cutoff_utc,
        "candidate": candidate_row,
        "benchmark": benchmark_row,
        "incumbents": incumbent_rows,
        "return_bundle": bundle_ref,
        "reason_codes": sorted(set(reason_codes or [])),
        "net_of_modeled_costs": True,
        "contains_future_data": False,
        "contains_account_data": False,
        "actual_orders_count": 0,
        "draft_sha256": "",
    }
    document["draft_sha256"] = sha256_obj(_payload(document, "draft_sha256"))
    validation = validate_draft(document, plan=plan, repository_root=repository_root,
                                policy_path=policy_path, revalidate_sources=False)
    if validation:
        raise ShadowError("draft_self_validation:" + validation[0])
    return document


def validate_draft(document: Any, *, plan: dict, repository_root: Path = ROOT,
                   policy_path: Path = POLICY_DEFAULT,
                   revalidate_sources: bool = True) -> list[str]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return ["draft_policy_invalid"]
    if not isinstance(document, dict):
        return ["draft_not_object"]
    errors: list[str] = []
    if set(document) != _DRAFT_KEYS:
        errors.append("draft_keys_invalid")
    if (document.get("schema") != DRAFT_SCHEMA or document.get("tool") != TOOL
            or document.get("plan_id") != plan.get("plan_id")
            or document.get("plan_sha256") != plan.get("plan_sha256")):
        errors.append("draft_identity_invalid")
    day = parse_date(document.get("session_date"))
    generated = parse_utc(document.get("generated_at_utc"))
    cutoff = parse_utc(document.get("data_cutoff_utc"))
    if day is None:
        errors.append("draft_session_date_invalid")
    else:
        try:
            close = _session_close(day)
            if cutoff is not None and cutoff < close:
                errors.append("draft_cutoff_before_session_close")
        except ShadowError:
            errors.append("draft_session_calendar_invalid")
    if generated is None or cutoff is None or (generated is not None and cutoff is not None
                                                and generated < cutoff):
        errors.append("draft_timestamps_invalid")
    state = document.get("state")
    if state not in {"complete", "unavailable"}:
        errors.append("draft_state_invalid")
    expected_ids = [plan["candidate"]["strategy_id"], plan["benchmark"]["strategy_id"],
                    *[row["strategy_id"] for row in plan["incumbents"]]]
    rows = [document.get("candidate"), document.get("benchmark")]
    incumbents = document.get("incumbents")
    if not isinstance(incumbents, list):
        errors.append("draft_incumbents_invalid")
        incumbents = []
    rows.extend(incumbents)
    observed_ids = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _OBS_KEYS:
            errors.append(f"draft_observation_invalid:{index}")
            continue
        observed_ids.append(row.get("strategy_id"))
        value, active, exposure = row.get("net_return"), row.get("active"), row.get(
            "gross_exposure")
        if state == "complete":
            if (not _number(value) or abs(float(value)) > policy["maximum_absolute_daily_return"]
                    or type(active) is not bool or not _number(exposure)
                    or not 0 <= float(exposure) <= policy["maximum_gross_exposure"]):
                errors.append(f"draft_complete_value_invalid:{index}")
            elif active is not (float(exposure) > 1e-12):
                errors.append(f"draft_active_exposure_mismatch:{index}")
        elif any(value is not None for value in (value, active, exposure)):
            errors.append(f"draft_unavailable_value_not_null:{index}")
    if observed_ids != expected_ids:
        errors.append("draft_strategy_denominator_mismatch")
    bundle_ref = document.get("return_bundle")
    if state == "complete":
        if not isinstance(bundle_ref, dict) or set(bundle_ref) != _DRAFT_BUNDLE_KEYS:
            errors.append("draft_complete_return_bundle_invalid")
        else:
            try:
                path = _resolve(bundle_ref["path"], repository_root, policy)
                bundle = _read_json(path)
                bundle_errors = validate_return_bundle(
                    bundle, plan=plan, repository_root=repository_root,
                    policy_path=policy_path, revalidate_sources=revalidate_sources)
                if bundle_errors:
                    errors.append("draft_return_bundle_invalid:" + bundle_errors[0])
                expected = {
                    "path": bundle_ref["path"],
                    "file_sha256": sha256_file(path),
                    "bundle_sha256": bundle.get("bundle_sha256"),
                }
                if bundle_ref != expected:
                    errors.append("draft_return_bundle_binding_mismatch")
                if (document.get("session_date") != bundle.get("session_date")
                        or document.get("generated_at_utc") != bundle.get("generated_at_utc")
                        or document.get("data_cutoff_utc") != bundle.get("data_cutoff_utc")
                        or document.get("candidate") != bundle.get("candidate")
                        or document.get("benchmark") != bundle.get("benchmark")
                        or document.get("incumbents") != bundle.get("incumbents")):
                    errors.append("draft_return_bundle_rederivation_mismatch")
            except (KeyError, ShadowError):
                errors.append("draft_return_bundle_unavailable")
    elif bundle_ref is not None:
        errors.append("draft_unavailable_return_bundle_not_null")
    reasons = document.get("reason_codes")
    if (not isinstance(reasons, list) or reasons != sorted(set(reasons))
            or any(not isinstance(value, str) or not _TOKEN_RE.fullmatch(value)
                   for value in (reasons or []))):
        errors.append("draft_reason_codes_invalid")
    elif (state == "complete" and reasons) or (state == "unavailable" and not reasons):
        errors.append("draft_reason_codes_state_mismatch")
    if (document.get("net_of_modeled_costs") is not True
            or document.get("contains_future_data") is not False
            or document.get("contains_account_data") is not False
            or document.get("actual_orders_count") != 0):
        errors.append("draft_non_authority_contract_invalid")
    if document.get("draft_sha256") != sha256_obj(_payload(document, "draft_sha256")):
        errors.append("draft_hash_mismatch")
    return sorted(set(errors))


def _event(event_type: str, recorded_at_utc: str, payload: dict, events: list[dict],
           plan: dict, policy: dict) -> dict:
    document = {
        **SAFETY,
        "schema": EVENT_SCHEMA,
        "tool": TOOL,
        "plan_id": plan["plan_id"],
        "sequence": len(events),
        "event_type": event_type,
        "recorded_at_utc": recorded_at_utc,
        "previous_event_sha256": events[-1]["event_sha256"] if events else None,
        "policy_sha256": sha256_obj(policy),
        "plan_sha256": plan["plan_sha256"],
        "payload": payload,
        "event_sha256": "",
    }
    document["event_sha256"] = sha256_obj(_payload(document, "event_sha256"))
    return document


def parse_ledger_bytes(raw: bytes, *, plan: dict, repository_root: Path = ROOT,
                       policy_path: Path = POLICY_DEFAULT,
                       revalidate_sources: bool = True,
                       revalidate_drafts: bool = True) -> tuple[list[dict], list[str]]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return [], ["ledger_policy_invalid"]
    if not raw or len(raw) > policy["maximum_ledger_bytes"] or not raw.endswith(b"\n"):
        return [], ["ledger_empty_too_large_or_unterminated"]
    events: list[dict] = []
    for index, line in enumerate(raw.splitlines()):
        try:
            value = json.loads(line.decode("utf-8"))
        except Exception:
            return events, [f"ledger_line_invalid:{index}"]
        events.append(value)
    return events, validate_events(events, plan=plan, repository_root=repository_root,
                                   policy_path=policy_path,
                                   revalidate_sources=revalidate_sources,
                                   revalidate_drafts=revalidate_drafts)


def read_ledger(path: Path, *, plan: dict, repository_root: Path = ROOT,
                policy_path: Path = POLICY_DEFAULT,
                revalidate_sources: bool = True,
                revalidate_drafts: bool = True) -> tuple[list[dict], list[str]]:
    policy, errors = load_policy(policy_path)
    if policy is None or errors:
        return [], ["ledger_policy_invalid"]
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > policy[
                "maximum_ledger_bytes"]:
            raise ShadowError("ledger_not_regular_bounded_file")
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            raw = handle.read(policy["maximum_ledger_bytes"] + 1)
    except Exception:
        return [], ["ledger_unreadable"]
    return parse_ledger_bytes(raw, plan=plan, repository_root=repository_root,
                              policy_path=policy_path,
                              revalidate_sources=revalidate_sources,
                              revalidate_drafts=revalidate_drafts)


def _embedded_observation_errors(payload: dict, plan: dict, policy: dict,
                                 tag: str) -> list[str]:
    errors: list[str] = []
    if not _safe_relative(payload.get("draft_path"), policy["allowed_input_roots"]):
        errors.append(f"ledger_draft_path_invalid:{tag}")
    for key in ("draft_file_sha256", "draft_sha256"):
        if not isinstance(payload.get(key), str) or not _SHA_RE.fullmatch(
                payload.get(key, "")):
            errors.append(f"ledger_draft_hash_invalid:{tag}:{key}")
    state = payload.get("state")
    if state not in {"complete", "unavailable"}:
        errors.append(f"ledger_state_invalid:{tag}")
    expected_ids = [plan["candidate"]["strategy_id"], plan["benchmark"]["strategy_id"],
                    *[row["strategy_id"] for row in plan["incumbents"]]]
    rows = [payload.get("candidate"), payload.get("benchmark")]
    incumbents = payload.get("incumbents")
    if not isinstance(incumbents, list):
        errors.append(f"ledger_incumbents_invalid:{tag}")
        incumbents = []
    rows.extend(incumbents)
    observed_ids: list[Any] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _OBS_KEYS:
            errors.append(f"ledger_observation_value_invalid:{tag}:{index}")
            continue
        observed_ids.append(row.get("strategy_id"))
        value, active, exposure = row.get("net_return"), row.get("active"), row.get(
            "gross_exposure")
        if state == "complete":
            if (not _number(value) or abs(float(value)) > policy["maximum_absolute_daily_return"]
                    or type(active) is not bool or not _number(exposure)
                    or not 0 <= float(exposure) <= policy["maximum_gross_exposure"]):
                errors.append(f"ledger_complete_value_invalid:{tag}:{index}")
            elif active is not (float(exposure) > 1e-12):
                errors.append(f"ledger_active_exposure_mismatch:{tag}:{index}")
        elif any(value is not None for value in (value, active, exposure)):
            errors.append(f"ledger_unavailable_value_not_null:{tag}:{index}")
    if observed_ids != expected_ids:
        errors.append(f"ledger_strategy_denominator_mismatch:{tag}")
    reasons = payload.get("reason_codes")
    if (not isinstance(reasons, list) or reasons != sorted(set(reasons))
            or any(not isinstance(value, str) or not _TOKEN_RE.fullmatch(value)
                   for value in (reasons or []))
            or (state == "complete" and bool(reasons))
            or (state == "unavailable" and not reasons)):
        errors.append(f"ledger_reason_codes_invalid:{tag}")
    return errors


def validate_events(events: Any, *, plan: dict, repository_root: Path = ROOT,
                    policy_path: Path = POLICY_DEFAULT,
                    revalidate_sources: bool = True,
                    revalidate_drafts: bool = True) -> list[str]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return ["ledger_policy_invalid"]
    if not isinstance(events, list) or not events:
        return ["ledger_events_missing"]
    errors: list[str] = []
    previous_hash = None
    previous_time = None
    previous_session: dt.date | None = None
    seen_drafts: set[str] = set()
    if len(events) > policy["maximum_observation_sessions"] + 1:
        errors.append("ledger_observation_limit_exceeded")
    for index, event in enumerate(events):
        tag = str(index)
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            errors.append(f"ledger_event_keys_invalid:{tag}")
            continue
        for key, expected in SAFETY.items():
            if event.get(key) != expected:
                errors.append(f"ledger_event_safety_invalid:{tag}:{key}")
        if (event.get("schema") != EVENT_SCHEMA or event.get("tool") != TOOL
                or event.get("plan_id") != plan.get("plan_id")
                or event.get("plan_sha256") != plan.get("plan_sha256")):
            errors.append(f"ledger_event_identity_invalid:{tag}")
        if event.get("sequence") != index or event.get("previous_event_sha256") != previous_hash:
            errors.append(f"ledger_event_chain_invalid:{tag}")
        if event.get("policy_sha256") != sha256_obj(policy):
            errors.append(f"ledger_event_policy_hash_invalid:{tag}")
        recorded = parse_utc(event.get("recorded_at_utc"))
        if recorded is None:
            errors.append(f"ledger_event_time_invalid:{tag}")
        elif previous_time is not None and recorded < previous_time:
            errors.append(f"ledger_event_time_not_monotonic:{tag}")
        else:
            previous_time = recorded
        if event.get("event_sha256") != sha256_obj(_payload(event, "event_sha256")):
            errors.append(f"ledger_event_hash_mismatch:{tag}")
        payload = event.get("payload")
        if index == 0:
            if event.get("event_type") != "plan_anchor" or not isinstance(
                    payload, dict) or set(payload) != _ANCHOR_KEYS:
                errors.append("ledger_anchor_invalid")
            else:
                try:
                    plan_path = _resolve(payload["plan_path"], repository_root, policy)
                    if (sha256_file(plan_path) != payload["plan_file_sha256"]
                            or payload["plan_sha256"] != plan.get("plan_sha256")):
                        errors.append("ledger_anchor_binding_mismatch")
                except (KeyError, ShadowError):
                    errors.append("ledger_anchor_source_invalid")
        elif event.get("event_type") != "observation" or not isinstance(
                payload, dict) or set(payload) != _OBS_EVENT_KEYS:
            errors.append(f"ledger_observation_event_invalid:{tag}")
        else:
            errors.extend(_embedded_observation_errors(payload, plan, policy, tag))
            day = parse_date(payload.get("session_date"))
            expected = parse_date(plan.get("start_session_date")) if previous_session is None \
                else next_session(previous_session)
            if day is None or day != expected:
                errors.append(f"ledger_session_spine_invalid:{tag}")
            else:
                previous_session = day
            draft_path_value = payload.get("draft_path")
            if draft_path_value in seen_drafts:
                errors.append(f"ledger_draft_reused:{tag}")
            if isinstance(draft_path_value, str):
                seen_drafts.add(draft_path_value)
            if revalidate_drafts:
                try:
                    draft_path = _resolve(draft_path_value, repository_root, policy)
                    draft = _read_json(draft_path)
                    draft_errors = validate_draft(
                        draft, plan=plan, repository_root=repository_root,
                        policy_path=policy_path, revalidate_sources=revalidate_sources)
                    if draft_errors:
                        errors.append(f"ledger_draft_invalid:{tag}:{draft_errors[0]}")
                    expected_payload = {
                        "session_date": draft.get("session_date"),
                        "state": draft.get("state"),
                        "draft_path": draft_path_value,
                        "draft_file_sha256": sha256_file(draft_path),
                        "draft_sha256": draft.get("draft_sha256"),
                        "candidate": draft.get("candidate"),
                        "benchmark": draft.get("benchmark"),
                        "incumbents": draft.get("incumbents"),
                        "reason_codes": draft.get("reason_codes"),
                    }
                    if payload != expected_payload:
                        errors.append(f"ledger_draft_binding_mismatch:{tag}")
                    generated = parse_utc(draft.get("generated_at_utc"))
                    if recorded is None or generated is None or recorded < generated:
                        errors.append(f"ledger_recorded_before_draft:{tag}")
                except (KeyError, ShadowError):
                    errors.append(f"ledger_draft_source_invalid:{tag}")
            if day is not None and recorded is not None:
                try:
                    close = _session_close(day)
                    if recorded < close:
                        errors.append(f"ledger_recorded_before_close:{tag}")
                    delay = (recorded - close).total_seconds() / 3600.0
                    if (payload.get("state") == "complete" and delay > policy[
                            "maximum_recording_delay_hours_for_complete"]):
                        errors.append(f"ledger_late_complete_backfill:{tag}")
                except ShadowError:
                    errors.append(f"ledger_session_close_invalid:{tag}")
        previous_hash = event.get("event_sha256") if isinstance(
            event.get("event_sha256"), str) else None
    return sorted(set(errors))


def init_ledger(*, plan_path: Path, ledger_path: Path, occurred_at_utc: str | None = None,
                repository_root: Path = ROOT,
                policy_path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(policy_path)
    if policy is None or errors:
        raise ShadowError("shadow_policy_invalid")
    portable = _portable(plan_path, repository_root)
    resolved_plan = _resolve(portable, repository_root, policy)
    plan = _read_json(resolved_plan)
    plan_errors = validate_plan(plan, repository_root=repository_root,
                                policy_path=policy_path)
    occurred_at_utc = _trusted_utc(repository_root, occurred_at_utc)
    occurred = parse_utc(occurred_at_utc)
    created = parse_utc(plan.get("created_at_utc"))
    if plan_errors:
        raise ShadowError("plan_invalid:" + plan_errors[0])
    if occurred is None or created is None or occurred < created:
        raise ShadowError("ledger_anchor_time_invalid")
    output = _output_path(ledger_path, repository_root, policy)
    event = _event("plan_anchor", occurred_at_utc, {
        "plan_path": portable,
        "plan_file_sha256": sha256_file(resolved_plan),
        "plan_sha256": plan["plan_sha256"],
    }, [], plan, policy)
    validation = validate_events([event], plan=plan, repository_root=repository_root,
                                 policy_path=policy_path)
    if validation:
        raise ShadowError("ledger_anchor_self_validation:" + validation[0])
    _atomic_write_once(output, canonical_bytes(event) + b"\n")
    return event


def append_draft(*, plan_path: Path, ledger_path: Path, draft_path: Path,
                 recorded_at_utc: str | None = None, repository_root: Path = ROOT,
                 policy_path: Path = POLICY_DEFAULT) -> dict:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        raise ShadowError("shadow_policy_invalid")
    plan_portable = _portable(plan_path, repository_root)
    plan_resolved = _resolve(plan_portable, repository_root, policy)
    plan = _read_json(plan_resolved)
    plan_errors = validate_plan(plan, repository_root=repository_root,
                                policy_path=policy_path, revalidate_source=False)
    if plan_errors:
        raise ShadowError("plan_invalid:" + plan_errors[0])
    draft_portable = _portable(draft_path, repository_root)
    draft_resolved = _resolve(draft_portable, repository_root, policy)
    draft = _read_json(draft_resolved)
    draft_errors = validate_draft(draft, plan=plan, repository_root=repository_root,
                                  policy_path=policy_path)
    if draft_errors:
        raise ShadowError("draft_invalid:" + draft_errors[0])
    recorded_at_utc = _trusted_utc(repository_root, recorded_at_utc)
    recorded = parse_utc(recorded_at_utc)
    generated = parse_utc(draft.get("generated_at_utc"))
    day = parse_date(draft.get("session_date"))
    if recorded is None or generated is None or day is None or recorded < generated:
        raise ShadowError("recorded_at_invalid")
    close = _session_close(day)
    if recorded < close:
        raise ShadowError("recorded_before_session_close")
    if (draft["state"] == "complete"
            and (recorded - close).total_seconds() / 3600.0
            > policy["maximum_recording_delay_hours_for_complete"]):
        raise ShadowError("late_complete_backfill_forbidden")
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise ShadowError("ledger_not_regular_file")
    with ledger_path.open("r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        raw = handle.read(policy["maximum_ledger_bytes"] + 1)
        events, ledger_errors = parse_ledger_bytes(
            raw, plan=plan, repository_root=repository_root, policy_path=policy_path,
            revalidate_sources=False, revalidate_drafts=False)
        if ledger_errors:
            raise ShadowError("ledger_invalid_before_append:" + ledger_errors[0])
        previous_session = parse_date(events[-1]["payload"].get("session_date")) \
            if len(events) > 1 else None
        expected = parse_date(plan["start_session_date"]) if previous_session is None \
            else next_session(previous_session)
        if day != expected:
            raise ShadowError("draft_not_next_market_session")
        if any(event.get("payload", {}).get("draft_path") == draft_portable
               for event in events[1:]):
            raise ShadowError("draft_already_appended")
        payload = {
            "session_date": draft["session_date"],
            "state": draft["state"],
            "draft_path": draft_portable,
            "draft_file_sha256": sha256_file(draft_resolved),
            "draft_sha256": draft["draft_sha256"],
            "candidate": draft["candidate"],
            "benchmark": draft["benchmark"],
            "incumbents": draft["incumbents"],
            "reason_codes": draft["reason_codes"],
        }
        event = _event("observation", recorded_at_utc, payload, events, plan, policy)
        validation = validate_events(
            [*events, event], plan=plan, repository_root=repository_root,
            policy_path=policy_path, revalidate_sources=False,
            revalidate_drafts=False)
        if validation:
            raise ShadowError("ledger_append_self_validation:" + validation[0])
        handle.seek(0, os.SEEK_END)
        handle.write(canonical_bytes(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def _compound(values: list[float]) -> float | None:
    if not values:
        return None
    nav = 1.0
    for value in values:
        nav *= 1.0 + value
    return nav - 1.0


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    nav = peak = 1.0
    worst = 0.0
    for value in values:
        nav *= 1.0 + value
        peak = max(peak, nav)
        worst = min(worst, nav / peak - 1.0)
    return worst


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    mean_left, mean_right = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    left_ss = sum((a - mean_left) ** 2 for a in left)
    right_ss = sum((b - mean_right) ** 2 for b in right)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def _jaccard(left: list[bool], right: list[bool]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    union = sum(a or b for a, b in zip(left, right))
    return None if union == 0 else sum(a and b for a, b in zip(left, right)) / union


def _autocovariance(values: list[float], lag: int, mean: float) -> float:
    return sum((values[index] - mean) * (values[index - lag] - mean)
               for index in range(lag, len(values))) / len(values)


def _hac(values: list[float], alpha: float, policy: dict) -> dict:
    n = len(values)
    lag = min(n - 1, max(0, math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))) \
        if n else 0
    if n < policy["minimum_hac_observations"]:
        return {"available": False, "reason": "sample_below_minimum", "n": n,
                "lag": lag, "mean": None, "standard_error": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    mean = statistics.fmean(values)
    variance = _autocovariance(values, 0, mean)
    if variance <= 0:
        return {"available": False, "reason": "nonpositive_long_run_variance", "n": n,
                "lag": lag, "mean": _round(mean), "standard_error": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    long_run = variance
    for current in range(1, lag + 1):
        weight = 1.0 - current / (lag + 1.0)
        long_run += 2.0 * weight * _autocovariance(values, current, mean)
    if long_run <= 0:
        return {"available": False, "reason": "nonpositive_long_run_variance", "n": n,
                "lag": lag, "mean": _round(mean), "standard_error": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    standard_error = math.sqrt(long_run / n)
    critical = statistics.NormalDist().inv_cdf(1.0 - alpha / 2.0)
    lower, upper = mean - critical * standard_error, mean + critical * standard_error
    return {
        "available": True,
        "reason": None,
        "n": n,
        "lag": lag,
        "two_sided_alpha": alpha,
        "critical_value": _round(critical),
        "mean": _round(mean),
        "standard_error": _round(standard_error),
        "confidence_interval": [_round(lower), _round(upper)],
        "lower_bound_above_zero": lower > 0,
    }


def _descriptive(rows: list[dict], policy: dict) -> dict:
    complete = [row for row in rows if row["state"] == "complete"]
    candidate = [float(row["candidate"]["net_return"]) for row in complete]
    benchmark = [float(row["benchmark"]["net_return"]) for row in complete]
    active = [bool(row["candidate"]["active"]) for row in complete]
    return {
        "elapsed_sessions": len(rows),
        "complete_sessions": len(complete),
        "unavailable_sessions": len(rows) - len(complete),
        "complete_fraction": _round(len(complete) / len(rows)) if rows else None,
        "candidate_active_sessions": sum(active),
        "candidate_active_fraction": _round(sum(active) / len(complete)) if complete else None,
        "candidate_total_return": _round(_compound(candidate), policy["output_decimal_places"]),
        "benchmark_total_return": _round(_compound(benchmark), policy["output_decimal_places"]),
        "candidate_excess_mean_daily_return": _round(
            statistics.fmean([a - b for a, b in zip(candidate, benchmark)]),
            policy["output_decimal_places"]) if complete else None,
        "candidate_positive_day_fraction": _round(
            sum(value > 0 for value in candidate) / len(candidate)) if candidate else None,
        "candidate_max_drawdown": _round(
            _max_drawdown(candidate), policy["output_decimal_places"]),
    }


def _checkpoint(rows: list[dict], count: int, alpha: float, policy: dict) -> dict:
    prefix = rows[:count]
    complete = [row for row in prefix if row["state"] == "complete"]
    candidate = [float(row["candidate"]["net_return"]) for row in complete]
    benchmark = [float(row["benchmark"]["net_return"]) for row in complete]
    incumbent_ids = [row["strategy_id"] for row in complete[0]["incumbents"]] \
        if complete else []
    incumbent_series = {
        strategy_id: [float(next(item["net_return"] for item in row["incumbents"]
                                 if item["strategy_id"] == strategy_id))
                      for row in complete]
        for strategy_id in incumbent_ids
    }
    incumbent_composite = [statistics.fmean(
        [float(item["net_return"]) for item in row["incumbents"]]) for row in complete]
    candidate_active = [bool(row["candidate"]["active"]) for row in complete]
    correlations = {strategy_id: _pearson(candidate, values)
                    for strategy_id, values in incumbent_series.items()}
    jaccards = {
        strategy_id: _jaccard(
            candidate_active,
            [bool(next(item["active"] for item in row["incumbents"]
                       if item["strategy_id"] == strategy_id)) for row in complete])
        for strategy_id in incumbent_ids
    }
    correlation_values = [abs(value) for value in correlations.values() if value is not None]
    jaccard_values = [value for value in jaccards.values() if value is not None]
    candidate_vs_benchmark = [a - b for a, b in zip(candidate, benchmark)]
    candidate_vs_incumbents = [a - b for a, b in zip(candidate, incumbent_composite)]
    return {
        "checkpoint_sessions": count,
        "two_sided_alpha": alpha,
        "alpha_spent_through_checkpoint": _round(sum(
            row["two_sided_alpha"] for row in policy[
                "decision_checkpoint_alpha_schedule"] if row["session_count"] <= count)),
        "prefix_metrics": _descriptive(prefix, policy),
        "candidate_vs_benchmark_hac": _hac(candidate_vs_benchmark, alpha, policy),
        "candidate_vs_incumbent_composite_hac": _hac(
            candidate_vs_incumbents, alpha, policy),
        "incumbent_correlations": {key: _round(value) for key, value in correlations.items()},
        "incumbent_active_day_jaccards": {key: _round(value) for key, value in jaccards.items()},
        "maximum_absolute_incumbent_correlation": _round(max(correlation_values))
        if len(correlation_values) == len(incumbent_ids) and incumbent_ids else None,
        "maximum_incumbent_active_day_jaccard": _round(max(jaccard_values))
        if len(jaccard_values) == len(incumbent_ids) and incumbent_ids else None,
    }


def _gate(code: str, passed: bool, observed: Any, required: Any,
          category: str) -> dict:
    return {"code": code, "category": category, "passed": bool(passed),
            "observed": observed, "required": required}


def build_summary(*, plan_path: Path, ledger_path: Path, as_of_utc: str | None = None,
                  repository_root: Path = ROOT,
                  policy_path: Path = POLICY_DEFAULT,
                  _verification_time: bool = False) -> dict:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        raise ShadowError("shadow_policy_invalid")
    if _verification_time:
        if as_of_utc is None:
            raise ShadowError("verification_time_missing")
    else:
        as_of_utc = _trusted_utc(repository_root, as_of_utc)
    as_of = parse_utc(as_of_utc)
    if as_of is None:
        raise ShadowError("as_of_utc_invalid")
    plan_portable = _portable(plan_path, repository_root)
    plan_resolved = _resolve(plan_portable, repository_root, policy)
    plan = _read_json(plan_resolved)
    plan_errors = validate_plan(plan, repository_root=repository_root,
                                policy_path=policy_path)
    if plan_errors:
        raise ShadowError("plan_invalid:" + plan_errors[0])
    events, ledger_errors = read_ledger(
        ledger_path, plan=plan, repository_root=repository_root, policy_path=policy_path,
        revalidate_sources=False, revalidate_drafts=True)
    if ledger_errors:
        raise ShadowError("ledger_invalid:" + ledger_errors[0])
    if any(parse_utc(event["recorded_at_utc"]) > as_of for event in events):
        raise ShadowError("as_of_before_ledger_event")
    rows = [event["payload"] for event in events[1:]]
    elapsed = len(rows)
    start = parse_date(plan["start_session_date"])
    if start is None:
        raise ShadowError("plan_start_date_invalid")
    expected_closed_dates = _closed_sessions(
        start, as_of, policy["maximum_observation_sessions"])
    observed_dates = [row["session_date"] for row in rows]
    denominator_complete = observed_dates == expected_closed_dates
    missing_session_dates = expected_closed_dates[len(observed_dates):] \
        if observed_dates == expected_closed_dates[:len(observed_dates)] else expected_closed_dates
    decision_schedule = policy["decision_checkpoint_alpha_schedule"]
    reached = [row for row in decision_schedule if row["session_count"] <= elapsed]
    current_checkpoint = reached[-1] if reached else None
    next_checkpoints = [value for value in (
        [*policy["descriptive_checkpoints_sessions"],
         *[row["session_count"] for row in decision_schedule]]) if value > elapsed]
    current_metrics = _descriptive(rows, policy)
    checkpoint = _checkpoint(rows, current_checkpoint["session_count"],
                             current_checkpoint["two_sided_alpha"], policy) \
        if current_checkpoint else {
            "checkpoint_sessions": None,
            "two_sided_alpha": None,
            "alpha_spent_through_checkpoint": 0.0,
            "prefix_metrics": None,
            "candidate_vs_benchmark_hac": None,
            "candidate_vs_incumbent_composite_hac": None,
            "incumbent_correlations": {},
            "incumbent_active_day_jaccards": {},
            "maximum_absolute_incumbent_correlation": None,
            "maximum_incumbent_active_day_jaccard": None,
        }
    gates: list[dict] = [
        _gate("session_denominator_complete", denominator_complete,
              {"observed": len(observed_dates), "expected": len(expected_closed_dates),
               "missing": len(missing_session_dates)},
              {"missing": 0}, "quality"),
    ]
    if current_checkpoint:
        metrics = checkpoint["prefix_metrics"]
        benchmark_hac = checkpoint["candidate_vs_benchmark_hac"]
        incumbent_hac = checkpoint["candidate_vs_incumbent_composite_hac"]
        gates.extend([
            _gate("complete_fraction", metrics["complete_fraction"] is not None
                  and metrics["complete_fraction"] >= policy["minimum_complete_fraction"],
                  metrics["complete_fraction"], policy["minimum_complete_fraction"], "quality"),
            _gate("candidate_active_fraction", metrics["candidate_active_fraction"] is not None
                  and metrics["candidate_active_fraction"] >= policy[
                      "minimum_candidate_active_fraction"],
                  metrics["candidate_active_fraction"],
                  policy["minimum_candidate_active_fraction"], "quality"),
            _gate("candidate_positive_total_return", metrics["candidate_total_return"] is not None
                  and metrics["candidate_total_return"] > policy[
                      "minimum_candidate_total_return"], metrics["candidate_total_return"],
                  f"> {policy['minimum_candidate_total_return']}", "return"),
            _gate("maximum_shadow_drawdown", metrics["candidate_max_drawdown"] is not None
                  and metrics["candidate_max_drawdown"] >= -policy["maximum_shadow_drawdown"],
                  metrics["candidate_max_drawdown"],
                  f">= {-policy['maximum_shadow_drawdown']}", "risk"),
            _gate("benchmark_excess_hac_available", benchmark_hac["available"] is True,
                  benchmark_hac["available"], True, "quality"),
            _gate("benchmark_excess_hac_lower_bound", benchmark_hac["available"] is True
                  and benchmark_hac["confidence_interval"][0] > policy[
                      "minimum_benchmark_excess_hac_lower_bound"],
                  benchmark_hac["confidence_interval"][0] if benchmark_hac[
                      "available"] else None,
                  f"> {policy['minimum_benchmark_excess_hac_lower_bound']}", "return"),
            _gate("incumbent_excess_hac_available", incumbent_hac["available"] is True,
                  incumbent_hac["available"], True, "quality"),
            _gate("incumbent_excess_hac_lower_bound", incumbent_hac["available"] is True
                  and incumbent_hac["confidence_interval"][0] > policy[
                      "minimum_incumbent_excess_hac_lower_bound"],
                  incumbent_hac["confidence_interval"][0] if incumbent_hac[
                      "available"] else None,
                  f"> {policy['minimum_incumbent_excess_hac_lower_bound']}", "uniqueness"),
            _gate("maximum_absolute_incumbent_correlation",
                  checkpoint["maximum_absolute_incumbent_correlation"] is not None
                  and checkpoint["maximum_absolute_incumbent_correlation"] <= policy[
                      "maximum_absolute_incumbent_correlation"],
                  checkpoint["maximum_absolute_incumbent_correlation"],
                  f"<= {policy['maximum_absolute_incumbent_correlation']}", "uniqueness"),
            _gate("maximum_incumbent_active_day_jaccard",
                  checkpoint["maximum_incumbent_active_day_jaccard"] is not None
                  and checkpoint["maximum_incumbent_active_day_jaccard"] <= policy[
                      "maximum_active_day_jaccard"],
                  checkpoint["maximum_incumbent_active_day_jaccard"],
                  f"<= {policy['maximum_active_day_jaccard']}", "uniqueness"),
        ])
    failed = [row["code"] for row in gates if row["passed"] is not True]
    quality_codes = {"session_denominator_complete", "complete_fraction",
                     "candidate_active_fraction",
                     "benchmark_excess_hac_available", "incumbent_excess_hac_available"}
    if not denominator_complete:
        status = "insufficient_quality"
    elif current_checkpoint is None:
        status = "collecting"
    elif any(code in quality_codes for code in failed):
        status = "insufficient_quality"
    elif failed:
        status = "shadow_evidence_not_supportive"
    else:
        status = "shadow_evidence_supportive_for_human_review"
    uniqueness_codes = {"incumbent_excess_hac_lower_bound",
                        "maximum_absolute_incumbent_correlation",
                        "maximum_incumbent_active_day_jaccard"}
    if current_checkpoint is None:
        uniqueness_status = "not_yet_evaluated"
    elif any(code in uniqueness_codes for code in failed):
        uniqueness_status = "not_preserved_at_preregistered_checkpoint"
    else:
        uniqueness_status = "preserved_at_preregistered_checkpoint"
    complete_fraction = current_metrics["complete_fraction"]
    reliability = "healthy" if (denominator_complete and (
        complete_fraction is None or complete_fraction >= policy[
            "minimum_complete_fraction"])) else "degraded"
    ledger_bytes = ledger_path.read_bytes()
    ledger_file_sha = hashlib.sha256(ledger_bytes).hexdigest()
    head = events[-1]["event_sha256"]
    cache_key = sha256_obj({"plan_sha256": plan["plan_sha256"],
                            "ledger_head_sha256": head, "as_of_utc": as_of_utc})
    progress = {
        "start_session_date": plan["start_session_date"],
        "latest_session_date": rows[-1]["session_date"] if rows else None,
        "elapsed_sessions": elapsed,
        "complete_sessions": current_metrics["complete_sessions"],
        "unavailable_sessions": current_metrics["unavailable_sessions"],
        "expected_closed_sessions_as_of": len(expected_closed_dates),
        "session_denominator_complete": denominator_complete,
        "missing_session_dates": missing_session_dates,
        "next_expected_session_date": (
            expected_closed_dates[len(observed_dates)]
            if len(observed_dates) < len(expected_closed_dates)
            else (next_session(parse_date(rows[-1]["session_date"])).isoformat()
                  if rows and len(rows) < policy["maximum_observation_sessions"]
                  else (plan["start_session_date"] if not rows else None))),
        "latest_reached_decision_checkpoint": current_checkpoint["session_count"]
        if current_checkpoint else None,
        "next_preregistered_checkpoint": min(next_checkpoints) if next_checkpoints else None,
        "maximum_observation_sessions": policy["maximum_observation_sessions"],
        "observation_horizon_complete": (
            len(expected_closed_dates) == policy["maximum_observation_sessions"]
            and denominator_complete),
    }
    compact = {
        "plan_id": plan["plan_id"],
        "status": status,
        "elapsed_sessions": elapsed,
        "complete_sessions": current_metrics["complete_sessions"],
        "expected_closed_sessions_as_of": len(expected_closed_dates),
        "session_denominator_complete": denominator_complete,
        "missing_session_count": len(missing_session_dates),
        "checkpoint_sessions": checkpoint["checkpoint_sessions"],
        "next_checkpoint": progress["next_preregistered_checkpoint"],
        "candidate_total_return": current_metrics["candidate_total_return"],
        "candidate_max_drawdown": current_metrics["candidate_max_drawdown"],
        "failed_gate_codes": failed,
        "profitability_status": "not_established",
        "execution_enabled": False,
    }
    if len(canonical_bytes(compact)) > policy["maximum_compact_context_bytes"]:
        raise ShadowError("compact_context_too_large")
    document = {
        **SAFETY,
        "schema": SUMMARY_SCHEMA,
        "tool": TOOL,
        "ok": True,
        "generated_at_utc": as_of_utc,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "ledger_file_sha256": ledger_file_sha,
        "ledger_head_sha256": head,
        "cache_key": cache_key,
        "status": status,
        "observation_progress": progress,
        "checkpoint_evaluation": checkpoint,
        "current_metrics": current_metrics,
        "quality_gates": gates,
        "failed_gate_codes": failed,
        "reliability_status": reliability,
        "uniqueness_status": uniqueness_status,
        "profitability_status": "not_established",
        "eligible_for_human_shadow_evidence_review": (
            status == "shadow_evidence_supportive_for_human_review"),
        "compact_context": compact,
        "limitations": LIMITATIONS,
        "actual_orders_count": 0,
        "summary_sha256": "",
    }
    document["summary_sha256"] = sha256_obj(_payload(document, "summary_sha256"))
    summary_errors = validate_summary(document, policy_path=policy_path)
    if summary_errors:
        raise ShadowError("summary_self_validation:" + summary_errors[0])
    return document


def validate_summary(document: Any, *, policy_path: Path = POLICY_DEFAULT) -> list[str]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return ["summary_policy_invalid"]
    if not isinstance(document, dict):
        return ["summary_not_object"]
    errors: list[str] = []
    if set(document) != _SUMMARY_KEYS:
        errors.append("summary_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"summary_safety_invalid:{key}")
    if (document.get("schema") != SUMMARY_SCHEMA or document.get("tool") != TOOL
            or document.get("ok") is not True or parse_utc(
                document.get("generated_at_utc")) is None):
        errors.append("summary_identity_invalid")
    for key in ("plan_sha256", "ledger_file_sha256", "ledger_head_sha256", "cache_key",
                "summary_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                document.get(key, "")):
            errors.append(f"summary_hash_invalid:{key}")
    status = document.get("status")
    allowed = {"collecting", "insufficient_quality", "shadow_evidence_not_supportive",
               "shadow_evidence_supportive_for_human_review"}
    if status not in allowed:
        errors.append("summary_status_invalid")
    progress = document.get("observation_progress")
    if not isinstance(progress, dict) or set(progress) != _PROGRESS_KEYS:
        errors.append("summary_progress_shape_invalid")
    elif (not isinstance(progress.get("elapsed_sessions"), int)
          or not isinstance(progress.get("expected_closed_sessions_as_of"), int)
          or not isinstance(progress.get("session_denominator_complete"), bool)
          or not isinstance(progress.get("missing_session_dates"), list)
          or len(progress["missing_session_dates"]) > policy["maximum_observation_sessions"]):
        errors.append("summary_progress_values_invalid")
    current = document.get("current_metrics")
    if not isinstance(current, dict) or set(current) != _DESCRIPTIVE_KEYS:
        errors.append("summary_current_metrics_shape_invalid")
    checkpoint = document.get("checkpoint_evaluation")
    if not isinstance(checkpoint, dict) or set(checkpoint) != _CHECKPOINT_KEYS:
        errors.append("summary_checkpoint_shape_invalid")
    else:
        prefix = checkpoint.get("prefix_metrics")
        if prefix is not None and (not isinstance(prefix, dict)
                                   or set(prefix) != _DESCRIPTIVE_KEYS):
            errors.append("summary_checkpoint_prefix_shape_invalid")
        for key in ("candidate_vs_benchmark_hac",
                    "candidate_vs_incumbent_composite_hac"):
            value = checkpoint.get(key)
            if value is not None and (not isinstance(value, dict)
                                      or (set(value) != _HAC_BASE_KEYS
                                          and set(value) != _HAC_AVAILABLE_KEYS)):
                errors.append(f"summary_hac_shape_invalid:{key}")
        for key in ("incumbent_correlations", "incumbent_active_day_jaccards"):
            value = checkpoint.get(key)
            if (not isinstance(value, dict) or len(value) > 20
                    or any(not isinstance(strategy_id, str)
                           or not _ID_RE.fullmatch(strategy_id)
                           or (metric is not None and not _number(metric))
                           for strategy_id, metric in value.items())):
                errors.append(f"summary_dependence_shape_invalid:{key}")
    gates = document.get("quality_gates")
    if not isinstance(gates, list):
        errors.append("summary_gates_invalid")
        gates = []
    elif (len(gates) > 20 or any(not isinstance(row, dict) or set(row) != _GATE_KEYS
                                 or not isinstance(row.get("code"), str)
                                 or row.get("category") not in {
                                     "quality", "return", "risk", "uniqueness"}
                                 or type(row.get("passed")) is not bool for row in gates)):
        errors.append("summary_gate_shape_invalid")
    failed = [row.get("code") for row in gates if isinstance(row, dict)
              and row.get("passed") is not True]
    if document.get("failed_gate_codes") != failed:
        errors.append("summary_failed_codes_mismatch")
    eligible = status == "shadow_evidence_supportive_for_human_review"
    if document.get("eligible_for_human_shadow_evidence_review") is not eligible:
        errors.append("summary_eligibility_mismatch")
    if (document.get("profitability_status") != "not_established"
            or document.get("actual_orders_count") != 0
            or document.get("limitations") != LIMITATIONS):
        errors.append("summary_non_authority_contract_invalid")
    compact = document.get("compact_context")
    if (not isinstance(compact, dict) or set(compact) != _COMPACT_KEYS
            or len(canonical_bytes(compact)) > policy[
            "maximum_compact_context_bytes"]):
        errors.append("summary_compact_context_invalid")
    elif (compact.get("status") != status
          or compact.get("failed_gate_codes") != document.get("failed_gate_codes")
          or compact.get("profitability_status") != "not_established"
          or compact.get("execution_enabled") is not False):
        errors.append("summary_compact_context_mismatch")
    if document.get("summary_sha256") != sha256_obj(_payload(document, "summary_sha256")):
        errors.append("summary_hash_mismatch")
    return sorted(set(errors))


def verify_summary(*, summary: dict, plan_path: Path, ledger_path: Path,
                   repository_root: Path = ROOT,
                   policy_path: Path = POLICY_DEFAULT) -> list[str]:
    errors = validate_summary(summary, policy_path=policy_path)
    if errors:
        return errors
    try:
        rebuilt = build_summary(
            plan_path=plan_path, ledger_path=ledger_path,
            as_of_utc=summary["generated_at_utc"], repository_root=repository_root,
            policy_path=policy_path, _verification_time=True)
    except ShadowError as exc:
        return [f"summary_rebuild_failed:{exc}"]
    return [] if rebuilt == summary else ["summary_semantic_rebuild_mismatch"]


def _refuse(error: str) -> dict:
    return {**SAFETY, "tool": TOOL, "ok": False, "error": error,
            "profitability_status": "not_established", "actual_orders_count": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prospective strategy shadow observation; research-only and no execution.")
    parser.add_argument("--policy", default=str(POLICY_DEFAULT))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-policy")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--evidence-gate", required=True)
    plan_parser.add_argument("--start-date", required=True)
    plan_parser.add_argument("--approval-reference", required=True)
    plan_parser.add_argument("--out", required=True)
    plan_parser.add_argument("--repo-root", default=str(ROOT))
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--plan", required=True)
    init_parser.add_argument("--ledger", required=True)
    init_parser.add_argument("--repo-root", default=str(ROOT))
    bundle_parser = sub.add_parser("bundle")
    bundle_parser.add_argument("--plan", required=True)
    bundle_parser.add_argument("--session", required=True)
    bundle_parser.add_argument("--candidate-result", required=True)
    bundle_parser.add_argument("--benchmark-result", required=True)
    bundle_parser.add_argument("--incumbent-result", action="append", default=[])
    bundle_parser.add_argument("--out", required=True)
    bundle_parser.add_argument("--repo-root", default=str(ROOT))
    draft_parser = sub.add_parser("draft")
    draft_parser.add_argument("--plan", required=True)
    draft_parser.add_argument("--session", required=True)
    draft_parser.add_argument("--state", required=True, choices=["complete", "unavailable"])
    draft_parser.add_argument("--bundle")
    draft_parser.add_argument("--reason", action="append", default=[])
    draft_parser.add_argument("--out", required=True)
    draft_parser.add_argument("--repo-root", default=str(ROOT))
    append_parser = sub.add_parser("append")
    append_parser.add_argument("--plan", required=True)
    append_parser.add_argument("--ledger", required=True)
    append_parser.add_argument("--draft", required=True)
    append_parser.add_argument("--repo-root", default=str(ROOT))
    summary_parser = sub.add_parser("summarize")
    summary_parser.add_argument("--plan", required=True)
    summary_parser.add_argument("--ledger", required=True)
    summary_parser.add_argument("--out")
    summary_parser.add_argument("--repo-root", default=str(ROOT))
    validate_plan_parser = sub.add_parser("validate-plan")
    validate_plan_parser.add_argument("--input", required=True)
    validate_plan_parser.add_argument("--repo-root", default=str(ROOT))
    validate_draft_parser = sub.add_parser("validate-draft")
    validate_draft_parser.add_argument("--input", required=True)
    validate_draft_parser.add_argument("--plan", required=True)
    validate_draft_parser.add_argument("--repo-root", default=str(ROOT))
    validate_bundle_parser = sub.add_parser("validate-bundle")
    validate_bundle_parser.add_argument("--input", required=True)
    validate_bundle_parser.add_argument("--plan", required=True)
    validate_bundle_parser.add_argument("--repo-root", default=str(ROOT))
    validate_ledger_parser = sub.add_parser("validate-ledger")
    validate_ledger_parser.add_argument("--input", required=True)
    validate_ledger_parser.add_argument("--plan", required=True)
    validate_ledger_parser.add_argument("--repo-root", default=str(ROOT))
    verify_parser = sub.add_parser("verify-summary")
    verify_parser.add_argument("--input", required=True)
    verify_parser.add_argument("--plan", required=True)
    verify_parser.add_argument("--ledger", required=True)
    verify_parser.add_argument("--repo-root", default=str(ROOT))
    args = parser.parse_args(argv)
    policy_path = Path(args.policy)
    try:
        if args.command == "check-policy":
            policy, errors = load_policy(policy_path)
            output = {**SAFETY, "tool": TOOL, "ok": policy is not None and not errors,
                      "errors": errors, "policy_sha256": sha256_obj(policy)
                      if policy is not None else None,
                      "profitability_status": "not_established", "actual_orders_count": 0}
            code = 0 if output["ok"] else 1
        elif args.command == "plan":
            root = Path(args.repo_root)
            output = build_plan(
                evidence_gate_path=Path(args.evidence_gate),
                start_session_date=args.start_date,
                approval_reference=args.approval_reference,
                repository_root=root,
                policy_path=policy_path,
            )
            out = _output_path(Path(args.out), root, load_policy(policy_path)[0])
            _write_json_once(out, output)
            code = 0
        elif args.command == "init":
            output = init_ledger(
                plan_path=Path(args.plan), ledger_path=Path(args.ledger),
                repository_root=Path(args.repo_root),
                policy_path=policy_path)
            code = 0
        elif args.command == "bundle":
            root = Path(args.repo_root)
            plan = _read_json(Path(args.plan))
            incumbent_paths: dict[str, Path] = {}
            for value in args.incumbent_result:
                if "=" not in value:
                    raise ShadowError("incumbent_result_must_be_strategy_id_equals_path")
                strategy_id, path = value.split("=", 1)
                if strategy_id in incumbent_paths:
                    raise ShadowError("incumbent_result_duplicate_strategy_id")
                incumbent_paths[strategy_id] = Path(path)
            source_paths = {
                plan["candidate"]["strategy_id"]: Path(args.candidate_result),
                plan["benchmark"]["strategy_id"]: Path(args.benchmark_result),
                **incumbent_paths,
            }
            output = build_return_bundle(
                plan=plan, session_date=args.session, source_paths=source_paths,
                repository_root=root, policy_path=policy_path)
            out = _output_path(Path(args.out), root, load_policy(policy_path)[0])
            _write_json_once(out, output)
            code = 0
        elif args.command == "draft":
            root = Path(args.repo_root)
            plan = _read_json(Path(args.plan))
            output = build_draft(
                plan=plan, session_date=args.session, state=args.state,
                return_bundle_path=Path(args.bundle) if args.bundle else None,
                reason_codes=args.reason, repository_root=root, policy_path=policy_path)
            out = _output_path(Path(args.out), root, load_policy(policy_path)[0])
            _write_json_once(out, output)
            code = 0
        elif args.command == "append":
            output = append_draft(
                plan_path=Path(args.plan), ledger_path=Path(args.ledger),
                draft_path=Path(args.draft),
                repository_root=Path(args.repo_root), policy_path=policy_path)
            code = 0
        elif args.command == "summarize":
            root = Path(args.repo_root)
            output = build_summary(
                plan_path=Path(args.plan), ledger_path=Path(args.ledger),
                repository_root=root, policy_path=policy_path)
            if args.out:
                policy, errors = load_policy(policy_path)
                if policy is None or errors:
                    raise ShadowError("shadow_policy_invalid")
                out = _output_path(Path(args.out), root, policy)
                output["cache_disposition"] = _write_idempotent(out, output)
                output.pop("cache_disposition")
            code = 0
        elif args.command == "validate-plan":
            document = _read_json(Path(args.input))
            errors = validate_plan(document, repository_root=Path(args.repo_root),
                                   policy_path=policy_path)
            output, code = {"ok": not errors, "errors": errors}, 0 if not errors else 1
        elif args.command == "validate-draft":
            plan = _read_json(Path(args.plan))
            document = _read_json(Path(args.input))
            errors = validate_draft(document, plan=plan, repository_root=Path(args.repo_root),
                                    policy_path=policy_path)
            output, code = {"ok": not errors, "errors": errors}, 0 if not errors else 1
        elif args.command == "validate-bundle":
            plan = _read_json(Path(args.plan))
            document = _read_json(Path(args.input))
            errors = validate_return_bundle(
                document, plan=plan, repository_root=Path(args.repo_root),
                policy_path=policy_path)
            output, code = {"ok": not errors, "errors": errors}, 0 if not errors else 1
        elif args.command == "validate-ledger":
            plan = _read_json(Path(args.plan))
            _events, errors = read_ledger(
                Path(args.input), plan=plan, repository_root=Path(args.repo_root),
                policy_path=policy_path)
            output, code = {"ok": not errors, "errors": errors}, 0 if not errors else 1
        else:
            summary = _read_json(Path(args.input))
            errors = verify_summary(
                summary=summary, plan_path=Path(args.plan), ledger_path=Path(args.ledger),
                repository_root=Path(args.repo_root), policy_path=policy_path)
            output, code = {"ok": not errors, "errors": errors}, 0 if not errors else 1
    except (OSError, KeyError, ValueError, ShadowError) as exc:
        output, code = _refuse(str(exc)), 1
    print(json.dumps(output, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
