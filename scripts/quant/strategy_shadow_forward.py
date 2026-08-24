#!/usr/bin/env python3
"""Strict caller-dated shadow-result state engine.

This module advances one normalized-capital research state by exactly one valid US
market session. Candidate and incumbent signals are derived only from an exact immutable
session feature package; replaceable aggregate corpora are discovery-only. The benchmark
is a continuous SPY reference. Caller timestamps are declarations: this module alone does
not prove a run was prospective or non-backfilled, and ``write_json_once`` is not a
canonical plan/strategy/session singleton. Those properties require a trusted outer clock
receipt and canonical singleton registry. It has no network, model, broker, account, order,
scheduling, production, Buy, freeze, or risk-authority surface.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
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
from quant import cost_model  # noqa: E402
from quant import feature_corpus  # noqa: E402
from quant import features  # noqa: E402
from quant import portfolio_backtest  # noqa: E402
from quant import session_capture  # noqa: E402

TOOL = "quant.strategy_shadow_forward"
POLICY_SCHEMA = "quant_strategy_shadow_forward_policy/1.0"
REPLAY_SPEC_SCHEMA = "quant_strategy_shadow_replay_spec/1.0"
FORWARD_RESULT_SCHEMA = "quant_strategy_shadow_forward_result/1.0"
RUN_SCHEMA = "quant_strategy_shadow_forward_run/1.0"
EXPECTED_RUN_DENOMINATOR_SCHEMA = "quant_strategy_shadow_expected_run_denominator/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_shadow_forward_policy.json"
COST_POLICY_DEFAULT = ROOT / "config" / "quant_cost_policy.json"
PORTFOLIO_POLICY_DEFAULT = ROOT / "config" / "quant_portfolio_policy.json"
CALENDAR_POLICY_DEFAULT = ROOT / "config" / "market_schedule.yaml"

PROGRAM_ID = "catalyst_continuation_baseline_v1"
PROGRAM_VERSION = "0.2.0"
ALGORITHM_VERSION = "catalyst_continuation_shadow_forward/1.0.0"
SUPPORTED_INSTRUMENTS = ("common_stock", "unleveraged_etf")
ROLES = ("candidate", "benchmark", "incumbent")
MAX_CHAIN_LENGTH = 504
MIN_MARKET_PRICE = 0.0001
MAX_MARKET_PRICE = 1_000_000_000.0

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_NUM = (int, float)
_ET = ZoneInfo("America/New_York")
_UTC = dt.timezone.utc

SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "feeds_production": False,
    "feeds_buy_gate": False,
    "feeds_freeze_gate": False,
    "feeds_risk_gate": False,
    "profitability_claim_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "actual_orders_count": 0,
    "network_invoked": False,
    "model_invoked": False,
}

LIMITATIONS = [
    "All entries, exits, costs, positions, and returns are hypothetical normalized-capital research observations.",
    "Daily OHLC bars cannot establish intraday sequence; a same-bar stop and target is modeled stop-first.",
    "Modeled costs, gap exits, and marks are assumptions, not observed fills or account performance.",
    "Planned stop risk includes frozen stop slippage and modeled round-trip costs but is not a loss guarantee; gaps, halts, and worse slippage can exceed it.",
    "An unavailable session carries the last verified state and is never treated as a valid zero-signal observation.",
    "Caller timestamps are declarations; this engine alone cannot prove prospective or non-backfilled creation, and write_json_once is not a canonical plan/strategy/session singleton. A trusted outer clock receipt and canonical singleton registry are required.",
    "The embedded expected-run denominator is a caller-frozen self-commitment. It detects omission from an already frozen spec set but does not prove plan approval or prevent a same-user writer from rebuilding and re-sealing the entire spec set; an approval-bound outer registry is required.",
    "This artifact cannot register, activate, promote, recommend, or execute a strategy or order.",
]

_EXPECTED_POLICY = {
    "objective": (
        "Generate deterministic, caller-dated, session-by-session normalized-capital "
        "shadow results from exact immutable point-in-time session packages; this engine "
        "does not itself prove prospective or non-backfilled creation."
    ),
    "supported_program": {
        "program_id": PROGRAM_ID,
        "program_version": PROGRAM_VERSION,
        "decision_clock": "prior_close",
        "entry_model": "next_open",
        "direction": "long",
        "instrument_types": list(SUPPORTED_INSTRUMENTS),
        "variant_parameter_bounds": {
            "max_entry_gap_atr": {"minimum_exclusive": 0.0, "maximum_inclusive": 5.0},
            "stop_atr": {"minimum_exclusive": 0.0, "maximum_inclusive": 10.0},
            "target_r_multiple": {"minimum_exclusive": 0.0, "maximum_inclusive": 20.0},
            "max_hold_sessions": {"minimum_inclusive": 1, "maximum_inclusive": 10},
        },
    },
    "allocation_policy": {
        "requested_weight": 0.1,
        "benchmark_weight": 1.0,
        "assumed_fraction_of_adv": 0.001,
        "priority_rule": "event_id_lexicographic",
        "sector_bucket_rule": "unknown_sector_single_concentration_bucket",
        "cost_rule": (
            "maximum_of_post_stress_frozen_floor_and_once_stressed_"
            "predecision_cost_estimate"
        ),
        "same_session_exit_proceeds_reusable": False,
    },
    "decision_rules": {
        "signal_source": "validated_exact_immutable_quant_session_feature_package_only",
        "signal_session_rule": "event_date_equals_forward_session",
        "level_rule": "prior_close_and_atr_14_with_frozen_variant_parameters",
        "max_entry_rule": (
            "next_open_plus_adverse_slippage_must_not_exceed_frozen_max_entry"
        ),
        "one_decision_per_source_row": True,
        "caller_scalar_return_allowed": False,
    },
    "eligibility_rules": {
        "feature_row_must_be_eligible": True,
        "feature_exclusion_reasons_must_be_empty": True,
        "instrument_must_be_supported": True,
        "prior_close_and_atr_14_must_be_positive": True,
        "missing_or_conflicting_session_input": "unavailable_never_zero_signal",
    },
    "simulation_semantics": {
        "long_only": True,
        "leverage_allowed": False,
        "shorting_allowed": False,
        "entry_cost_once": True,
        "exit_cost_once": True,
        "same_bar_stop_target_rule": "stop_first",
        "gap_through_stop_rule": "session_open_minus_frozen_stop_slippage",
        "target_rule": "frozen_target_minus_frozen_exit_slippage",
        "time_exit_rule": "close_on_max_hold_session_minus_frozen_exit_slippage",
        "mark_rule": "session_close",
        "benchmark_rule": (
            "continuous_spy_from_first_complete_session_entry_cost_once_no_forced_daily_exit"
        ),
        "unavailable_state_rule": "carry_last_verified_state_without_mark_or_cost",
    },
    "timestamp_policy": {
        "operating_timezone": "America/Los_Angeles",
        "market_timezone": "America/New_York",
        "maximum_recording_delay_hours_for_complete": 36,
        "cutoff_rule": "at_or_after_official_session_close_and_not_after_generation",
        "future_bar_rule": "reject_any_bar_after_forward_session",
    },
    "path_policy": {
        "maximum_input_bytes": 134217728,
        "maximum_output_bytes": 33554432,
        "symlinks_allowed": False,
        "path_escape_allowed": False,
        "write_rule": "write_once_idempotent_same_bytes_conflict_on_different_bytes",
    },
}

_POLICY_KEYS = {
    "schema", "version", "objective", "algorithm_version", "replay_spec_schema",
    "forward_result_schema", "run_schema", "supported_program", "allocation_policy",
    "decision_rules", "eligibility_rules", "simulation_semantics", "timestamp_policy",
    "path_policy", "safety",
}
_PROGRAM_KEYS = {
    "program_id", "program_version", "decision_clock", "entry_model", "direction",
    "instrument_types", "variant_parameter_bounds",
}
_PARAM_BOUND_KEYS = {"minimum_exclusive", "maximum_inclusive"}
_HOLD_BOUND_KEYS = {"minimum_inclusive", "maximum_inclusive"}
_ALLOCATION_POLICY_KEYS = {
    "requested_weight", "benchmark_weight", "assumed_fraction_of_adv", "priority_rule",
    "sector_bucket_rule", "cost_rule", "same_session_exit_proceeds_reusable",
}
_DECISION_RULE_KEYS = {
    "signal_source", "signal_session_rule", "level_rule", "max_entry_rule",
    "one_decision_per_source_row", "caller_scalar_return_allowed",
}
_ELIGIBILITY_RULE_KEYS = {
    "feature_row_must_be_eligible", "feature_exclusion_reasons_must_be_empty",
    "instrument_must_be_supported", "prior_close_and_atr_14_must_be_positive",
    "missing_or_conflicting_session_input",
}
_SEMANTIC_KEYS = {
    "long_only", "leverage_allowed", "shorting_allowed", "entry_cost_once",
    "exit_cost_once", "same_bar_stop_target_rule", "gap_through_stop_rule",
    "target_rule", "time_exit_rule", "mark_rule", "benchmark_rule",
    "unavailable_state_rule",
}
_TIMESTAMP_KEYS = {
    "operating_timezone", "market_timezone", "maximum_recording_delay_hours_for_complete",
    "cutoff_rule", "future_bar_rule",
}
_PATH_POLICY_KEYS = {
    "maximum_input_bytes", "maximum_output_bytes", "symlinks_allowed",
    "path_escape_allowed", "write_rule",
}

_REPLAY_SPEC_KEYS = {
    "schema", "tool", "algorithm_version", "created_at_utc", "start_session_date",
    "plan_id", "plan_sha256", "strategy_id", "role", "strategy_mode", "program",
    "variant_parameters", "simulation_assumptions", "allocation", "decision_rules",
    "eligibility_rules", "simulation_semantics", "policy_contracts",
    "historical_anchors", "expected_run_denominator", "supported_instruments",
    "safety_constants",
    "replay_spec_sha256",
}
_VARIANT_KEYS = {
    "max_entry_gap_atr", "stop_atr", "target_r_multiple", "max_hold_sessions",
}
_ASSUMPTION_KEYS = {
    "entry_slippage_bps", "exit_slippage_bps", "stop_slippage_bps",
    "transaction_cost_bps_per_side", "cost_stress_multiplier",
}
_ALLOCATION_KEYS = {"requested_weight", "benchmark_weight", "assumed_fraction_of_adv"}
_POLICY_CONTRACT_KEYS = {
    "allocation_policy", "cost_policy", "portfolio_policy", "calendar_policy",
}
_POLICY_REF_KEYS = {
    "name", "path", "schema", "version", "file_sha256", "semantic_sha256",
}
_ANCHOR_KEYS = {
    "source_backtest_plan_sha256", "portfolio_plan_sha256",
    "historical_result_sha256", "historical_strategy_contract_sha256",
}
_EXPECTED_RUN_DENOMINATOR_KEYS = {
    "schema", "plan_id", "plan_sha256", "candidate_strategy_id",
    "benchmark_strategy_id", "incumbent_strategy_ids", "expected_strategy_ids",
    "expected_count", "commitment_sha256",
}

_INPUT_REF_KEYS = {
    "role", "path", "status", "file_sha256", "semantic_sha256", "schema", "error",
}
_MANIFEST_KEYS = {
    "source_kind", "entries", "entry_count", "previous_result_included",
    "replay_spec_sha256", "policy_contracts_sha256", "manifest_sha256",
}
_STATE_KEYS = {
    "as_of_session_date", "state_data_status", "normalized_nav", "cash",
    "gross_market_value", "gross_exposure", "net_exposure", "aggregate_stop_risk",
    "cumulative_modeled_cost", "open_position_count", "open_positions", "state_sha256",
}
_POSITION_KEYS = {
    "position_id", "position_kind", "event_id", "symbol", "instrument_type", "sector",
    "catalyst_type", "entry_session_date", "entry_price", "entry_notional", "entry_cost",
    "cost_rate_per_side", "max_entry", "stop_price", "target_price", "units",
    "last_mark_price", "market_value", "held_sessions", "max_hold_sessions",
    "planned_stop_risk", "position_sha256",
}
_DENOMINATOR_KEYS = {
    "source_status", "source_row_count", "eligible_row_count", "ineligible_row_count",
    "generated_signal_count", "entered_signal_count", "excluded_signal_count",
    "decision_count", "source_event_ids", "zero_signal_session", "denominator_sha256",
}
_LEVEL_KEYS = {"prior_close", "atr_14", "max_entry", "modeled_entry", "stop", "target"}
_DECISION_COST_KEYS = {
    "status", "estimate_sha256", "estimated_bps_per_side",
    "frozen_floor_bps_per_side", "applied_bps_per_side",
}
_DECISION_KEYS = {
    "decision_id", "event_id", "symbol", "instrument_type", "feature_row_sha256",
    "priority", "eligible_signal", "status", "reason_codes", "levels", "cost",
    "allocated_weight", "position_id", "decision_sha256",
}
_POSITION_EVENT_KEYS = {
    "position_id", "event_id", "symbol", "was_entered_this_session", "action",
    "exit_reason", "exit_price", "mark_price", "gross_value", "exit_cost",
    "held_sessions", "event_sha256",
}
_NAV_ROW_KEYS = {
    "point", "session_date", "normalized_nav", "cash", "gross_market_value",
    "gross_exposure", "state_sha256", "session_return", "return_available",
    "entry_cost", "exit_cost", "row_sha256",
}
_MODELED_COST_KEYS = {
    "entry_cost", "exit_cost", "total_session_cost", "cumulative_modeled_cost",
    "entry_count", "exit_count",
}
_RESULT_KEYS = set(SAFETY) | {
    "schema", "tool", "algorithm_version", "artifact_valid", "plan_id", "plan_sha256",
    "strategy_id", "role", "session_date", "session_close_utc", "generated_at_utc",
    "data_cutoff_utc", "status", "reason_codes", "replay_spec_sha256",
    "previous_result_sha256", "previous_terminal_state_sha256", "input_manifest",
    "signal_denominator", "decisions", "position_events", "opening_state",
    "terminal_state", "nav_rows", "net_return", "gross_exposure", "active",
    "modeled_costs", "limitations", "result_sha256",
}

_RESULT_REF_KEYS = {
    "role", "strategy_id", "path", "file_sha256", "result_sha256",
    "replay_spec_sha256", "status",
}
_RUN_DENOMINATOR_KEYS = {
    "candidate_strategy_id", "benchmark_strategy_id", "incumbent_strategy_ids",
    "expected_strategy_ids", "expected_count", "result_count", "all_present_once",
    "expected_run_denominator_sha256", "denominator_sha256",
}
_RUN_KEYS = set(SAFETY) | {
    "schema", "tool", "algorithm_version", "artifact_valid", "plan_id", "plan_sha256",
    "session_date", "generated_at_utc", "status", "reason_codes", "candidate",
    "benchmark", "incumbents", "expected_run_denominator_sha256",
    "result_denominator", "results_sha256", "limitations", "run_sha256",
}


class ForwardError(RuntimeError):
    """Fail-closed contract or security violation."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path, *, root: Path | None = None,
                maximum_bytes: int = 512 * 1024 * 1024) -> str:
    """Hash one stable, bounded, regular, single-link file snapshot."""
    raw, _portable = _stable_read_bytes(
        path, root if root is not None else Path(path).parent, maximum_bytes)
    return hashlib.sha256(raw).hexdigest()


def _payload(document: dict, hash_key: str) -> dict:
    return {key: value for key, value in document.items() if key != hash_key}


def _number(value: Any, *, positive: bool = False, nonnegative: bool = False) -> bool:
    if not isinstance(value, _NUM) or isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(numeric):
        return False
    if positive and numeric <= 0:
        return False
    if nonnegative and numeric < 0:
        return False
    return True


def _round(value: float | int | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ForwardError("nonfinite_derived_value")
    result = round(numeric, 10)
    return 0.0 if result == 0 else result


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    return parsed.astimezone(_UTC)


def _parse_canonical_utc(value: Any) -> dt.datetime | None:
    parsed = _parse_utc(value)
    if parsed is None or _format_utc(parsed) != value:
        return None
    return parsed


def _format_utc(value: dt.datetime) -> str:
    return value.astimezone(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _trusted_utc(value: str | None) -> str:
    if value is None:
        return _format_utc(dt.datetime.now(_UTC))
    parsed = _parse_utc(value)
    if parsed is None:
        raise ForwardError("utc_timestamp_invalid")
    return _format_utc(parsed)


def _calendar_from_bytes(raw: bytes) -> tuple[set[str], set[str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ForwardError("market_calendar_not_utf8") from exc
    holidays: set[str] = set()
    early: set[str] = set()
    section: str | None = None
    date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for line in text.splitlines():
        stripped = line.strip()
        if line and not line[0].isspace() and stripped.endswith(":"):
            key = stripped[:-1]
            section = ("holiday" if key.startswith("holidays") else
                       "early" if key.startswith("early_close") else None)
            continue
        if section and stripped.startswith("-"):
            match = date_pattern.search(stripped)
            if match is not None and _parse_date(match.group(1)) is not None:
                (holidays if section == "holiday" else early).add(match.group(1))
    return holidays, early


def _root_for_authority_path(path: Path) -> Path:
    candidate = Path(path).absolute()
    try:
        candidate.relative_to(ROOT.absolute())
        return ROOT
    except ValueError:
        return candidate.parent


def _read_calendar(path: Path, root: Path) -> tuple[set[str], set[str], bytes, str]:
    raw, portable = _stable_read_bytes(path, root, 2 * 1024 * 1024)
    holidays, early = _calendar_from_bytes(raw)
    if not holidays:
        raise ForwardError("market_calendar_empty")
    return holidays, early, raw, portable


def _calendar(path: Path = CALENDAR_POLICY_DEFAULT) -> tuple[set[str], set[str]]:
    holidays, early, _raw, _portable = _read_calendar(
        path, _root_for_authority_path(path))
    return holidays, early


def _session_verdict(day: dt.date, path: Path = CALENDAR_POLICY_DEFAULT) -> dict:
    holidays, early = _calendar(path)
    verdict = validate_market_day.verdict(day, holidays, early)
    if not verdict["calendar_covers_year"]:
        raise ForwardError("market_calendar_year_not_covered")
    return verdict


def _session_open(day: dt.date, path: Path = CALENDAR_POLICY_DEFAULT) -> dt.datetime:
    verdict = _session_verdict(day, path)
    if not verdict["is_trading_day"]:
        raise ForwardError("date_not_trading_session")
    return dt.datetime.combine(day, dt.time(9, 30), tzinfo=_ET).astimezone(_UTC)


def _session_close(day: dt.date, path: Path = CALENDAR_POLICY_DEFAULT) -> dt.datetime:
    verdict = _session_verdict(day, path)
    if not verdict["is_trading_day"]:
        raise ForwardError("date_not_trading_session")
    hour = 13 if verdict["is_early_close"] else 16
    return dt.datetime.combine(day, dt.time(hour), tzinfo=_ET).astimezone(_UTC)


def _next_session(day: dt.date, path: Path = CALENDAR_POLICY_DEFAULT) -> dt.date:
    current = day + dt.timedelta(days=1)
    for _ in range(16):
        if _session_verdict(current, path)["is_trading_day"]:
            return current
        current += dt.timedelta(days=1)
    raise ForwardError("next_market_session_not_found")


def _previous_session(day: dt.date, path: Path = CALENDAR_POLICY_DEFAULT) -> dt.date:
    current = day - dt.timedelta(days=1)
    for _ in range(16):
        if _session_verdict(current, path)["is_trading_day"]:
            return current
        current -= dt.timedelta(days=1)
    raise ForwardError("previous_market_session_not_found")


def _safe_root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ForwardError("root_not_regular_directory")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ForwardError("root_unresolvable") from exc


def _lexical_target(path: Path, root: Path) -> tuple[Path, Path]:
    resolved_root = _safe_root(root)
    path = Path(path)
    if not path.is_absolute() and ".." in path.parts:
        raise ForwardError("path_escape_forbidden")
    lexical_root = Path(root).absolute()
    lexical_candidate = path.absolute() if path.is_absolute() else lexical_root / path
    try:
        lexical_relative = lexical_candidate.relative_to(lexical_root)
        current = lexical_root
        for part in lexical_relative.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ForwardError("symlink_path_forbidden")
    except ValueError:
        pass
    candidate = lexical_candidate.resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ForwardError("path_outside_root") from exc
    return candidate, resolved_root


def _reject_symlink_components(candidate: Path, root: Path, *, include_leaf: bool) -> None:
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise ForwardError("path_outside_root") from exc
    current = root
    parts = relative.parts if include_leaf else relative.parts[:-1]
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ForwardError("symlink_path_forbidden")
        except OSError as exc:
            raise ForwardError("path_component_unreadable") from exc


def _relative_under_root(path: Path, root: Path) -> tuple[Path, Path]:
    """Return a purely lexical relative path and a trusted resolved directory root."""
    resolved_root = _safe_root(root)
    lexical_root = Path(root).absolute()
    value = Path(path)
    candidate = value.absolute() if value.is_absolute() else lexical_root / value
    try:
        relative = candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise ForwardError("path_outside_root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ForwardError("path_escape_forbidden")
    return relative, resolved_root


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _stable_read_bytes(path: Path, root: Path, maximum_bytes: int) -> tuple[bytes, str]:
    """Read exact bytes with openat/no-follow and pre/open/post identity checks."""
    if (not isinstance(maximum_bytes, int) or isinstance(maximum_bytes, bool)
            or maximum_bytes < 1):
        raise ForwardError("input_size_limit_invalid")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ForwardError("secure_nofollow_read_unavailable")
    relative, resolved_root = _relative_under_root(path, root)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        current_fd = os.open(resolved_root, directory_flags)
        descriptors.append(current_fd)
        for component in relative.parts[:-1]:
            current_fd = os.open(component, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > maximum_bytes):
            raise ForwardError("input_not_bounded_regular_single_link_file")
        file_fd = os.open(leaf, file_flags, dir_fd=current_fd)
        descriptors.append(file_fd)
        opened = os.fstat(file_fd)
        if _stable_metadata(before) != _stable_metadata(opened):
            raise ForwardError("input_changed_before_open")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise ForwardError("input_size_out_of_bounds")
        after = os.fstat(file_fd)
        post = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if (_stable_metadata(opened) != _stable_metadata(after)
                or _stable_metadata(after) != _stable_metadata(post)
                or total != after.st_size):
            raise ForwardError("input_changed_during_read")
        return b"".join(chunks), relative.as_posix()
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise ForwardError("secure_input_read_failed") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _strict_json_loads(raw: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("nonfinite_json_constant:" + value)

    def strict_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("nonfinite_json_number")
        return parsed

    def strict_int(value: str) -> int:
        if len(value.lstrip("-")) > 128:
            raise ValueError("oversized_json_integer")
        return int(value)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key:" + key)
            result[key] = value
        return result

    text = raw.decode("utf-8")
    return json.loads(text, parse_constant=reject_constant, parse_float=strict_float,
                      parse_int=strict_int, object_pairs_hook=unique_object)


def _read_json_strict(path: Path, root: Path, maximum_bytes: int
                      ) -> tuple[dict, str, bytes]:
    raw, portable = _stable_read_bytes(path, root, maximum_bytes)
    try:
        value = _strict_json_loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ForwardError("input_unreadable_or_malformed") from exc
    if not isinstance(value, dict):
        raise ForwardError("input_json_not_object")
    return value, portable, raw


def write_json_once(path: Path, document: dict, *, root: Path = ROOT,
                    maximum_bytes: int | None = None) -> str:
    """Write bytes once at a path; this is not a canonical strategy/session registry."""
    if not isinstance(document, dict):
        raise ForwardError("output_document_not_object")
    policy, errors = load_policy()
    if policy is None or errors:
        raise ForwardError("forward_policy_invalid")
    limit = maximum_bytes or policy["path_policy"]["maximum_output_bytes"]
    encoded = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True,
                          allow_nan=False) + "\n").encode("utf-8")
    if not encoded or len(encoded) > limit:
        raise ForwardError("output_size_out_of_bounds")
    candidate, resolved_root = _lexical_target(path, root)
    _reject_symlink_components(candidate, resolved_root, include_leaf=False)
    relative_parent = candidate.absolute().parent.relative_to(resolved_root)
    current = resolved_root
    for part in relative_parent.parts:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ForwardError("output_parent_not_regular_directory")
        else:
            current.mkdir()
        if current.is_symlink():
            raise ForwardError("symlink_path_forbidden")
    if candidate.exists() or candidate.is_symlink():
        try:
            existing, _portable = _stable_read_bytes(path, root, limit)
        except (FileNotFoundError, ForwardError) as exc:
            raise ForwardError("output_existing_not_stable_regular_single_link_file") from exc
        if existing == encoded:
            return "existing_identical"
        raise ForwardError("immutable_output_conflict")
    fd, temporary_name = tempfile.mkstemp(prefix=".tmp_shadow_forward_",
                                          suffix=".json", dir=str(candidate.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            try:
                existing, _portable = _stable_read_bytes(path, root, limit)
            except (FileNotFoundError, ForwardError) as exc:
                raise ForwardError("immutable_output_conflict") from exc
            if existing != encoded:
                raise ForwardError("immutable_output_conflict")
            return "existing_identical"
        directory_fd = os.open(candidate.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return "written"
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["forward_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("forward_policy_keys_invalid")
    fixed = {
        "schema": POLICY_SCHEMA,
        "version": "1.0.0",
        "algorithm_version": ALGORITHM_VERSION,
        "replay_spec_schema": REPLAY_SPEC_SCHEMA,
        "forward_result_schema": FORWARD_RESULT_SCHEMA,
        "run_schema": RUN_SCHEMA,
    }
    for key, expected in fixed.items():
        if document.get(key) != expected:
            errors.append(f"forward_policy_fixed_value_invalid:{key}")
    for key, expected in _EXPECTED_POLICY.items():
        if document.get(key) != expected:
            errors.append(f"forward_policy_immutable_contract_invalid:{key}")
    program = document.get("supported_program")
    if not isinstance(program, dict) or set(program) != _PROGRAM_KEYS:
        errors.append("forward_policy_program_shape_invalid")
    else:
        expected_program = {
            "program_id": PROGRAM_ID, "program_version": PROGRAM_VERSION,
            "decision_clock": "prior_close", "entry_model": "next_open", "direction": "long",
            "instrument_types": list(SUPPORTED_INSTRUMENTS),
        }
        for key, expected in expected_program.items():
            if program.get(key) != expected:
                errors.append(f"forward_policy_program_invalid:{key}")
        bounds = program.get("variant_parameter_bounds")
        if not isinstance(bounds, dict) or set(bounds) != _VARIANT_KEYS:
            errors.append("forward_policy_parameter_bounds_invalid")
        else:
            for name, value in bounds.items():
                expected_keys = _HOLD_BOUND_KEYS if name == "max_hold_sessions" else _PARAM_BOUND_KEYS
                if not isinstance(value, dict) or set(value) != expected_keys:
                    errors.append(f"forward_policy_parameter_bound_shape_invalid:{name}")
    allocation = document.get("allocation_policy")
    if not isinstance(allocation, dict) or set(allocation) != _ALLOCATION_POLICY_KEYS:
        errors.append("forward_policy_allocation_shape_invalid")
    elif (not _number(allocation.get("requested_weight"), positive=True)
          or float(allocation["requested_weight"]) > 1
          or not _number(allocation.get("benchmark_weight"), positive=True)
          or float(allocation["benchmark_weight"]) > 1
          or not _number(allocation.get("assumed_fraction_of_adv"), positive=True)
          or float(allocation["assumed_fraction_of_adv"]) > 0.01
          or allocation.get("priority_rule") != "event_id_lexicographic"
          or allocation.get("sector_bucket_rule") !=
              "unknown_sector_single_concentration_bucket"
          or allocation.get("cost_rule") !=
              "maximum_of_post_stress_frozen_floor_and_once_stressed_"
              "predecision_cost_estimate"
          or allocation.get("same_session_exit_proceeds_reusable") is not False):
        errors.append("forward_policy_allocation_invalid")
    expected_nested = (
        ("decision_rules", _DECISION_RULE_KEYS),
        ("eligibility_rules", _ELIGIBILITY_RULE_KEYS),
        ("simulation_semantics", _SEMANTIC_KEYS),
        ("timestamp_policy", _TIMESTAMP_KEYS),
        ("path_policy", _PATH_POLICY_KEYS),
    )
    for name, keys in expected_nested:
        value = document.get(name)
        if not isinstance(value, dict) or set(value) != keys:
            errors.append(f"forward_policy_nested_shape_invalid:{name}")
    if isinstance(document.get("decision_rules"), dict):
        rules = document["decision_rules"]
        if rules.get("one_decision_per_source_row") is not True or rules.get(
                "caller_scalar_return_allowed") is not False:
            errors.append("forward_policy_decision_safety_invalid")
    if isinstance(document.get("simulation_semantics"), dict):
        semantics = document["simulation_semantics"]
        if (semantics.get("long_only") is not True
                or semantics.get("leverage_allowed") is not False
                or semantics.get("shorting_allowed") is not False
                or semantics.get("entry_cost_once") is not True
                or semantics.get("exit_cost_once") is not True
                or semantics.get("same_bar_stop_target_rule") != "stop_first"):
            errors.append("forward_policy_simulation_safety_invalid")
    timestamp = document.get("timestamp_policy")
    if isinstance(timestamp, dict) and (
            timestamp.get("operating_timezone") != "America/Los_Angeles"
            or timestamp.get("market_timezone") != "America/New_York"
            or not isinstance(timestamp.get("maximum_recording_delay_hours_for_complete"), int)
            or isinstance(timestamp.get("maximum_recording_delay_hours_for_complete"), bool)
            or not 1 <= timestamp["maximum_recording_delay_hours_for_complete"] <= 72):
        errors.append("forward_policy_timestamp_invalid")
    paths = document.get("path_policy")
    if isinstance(paths, dict) and (
            not isinstance(paths.get("maximum_input_bytes"), int)
            or not 1024 <= paths["maximum_input_bytes"] <= 512 * 1024 * 1024
            or not isinstance(paths.get("maximum_output_bytes"), int)
            or not 1024 <= paths["maximum_output_bytes"] <= 128 * 1024 * 1024
            or paths.get("symlinks_allowed") is not False
            or paths.get("path_escape_allowed") is not False):
        errors.append("forward_policy_path_limits_invalid")
    if document.get("safety") != SAFETY:
        errors.append("forward_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT, *, root: Path = ROOT
                ) -> tuple[dict | None, list[str]]:
    try:
        document, _portable, _raw = _read_json_strict(path, root, 2 * 1024 * 1024)
    except (FileNotFoundError, ForwardError):
        return None, ["forward_policy_unreadable"]
    return document, validate_policy(document)


def _load_cost_policy(path: Path, root: Path) -> tuple[dict | None, list[str], bytes | None]:
    try:
        document, _portable, raw = _read_json_strict(path, root, 2 * 1024 * 1024)
    except (FileNotFoundError, ForwardError):
        return None, ["cost_policy_unreadable"], None
    try:
        errors = cost_model.validate_policy(document)
    except (KeyError, TypeError, ValueError, OverflowError):
        errors = ["cost_policy_malformed_nested_value"]
    return document, errors, raw


def _load_portfolio_policy(path: Path, root: Path
                           ) -> tuple[dict | None, list[str], bytes | None]:
    try:
        document, _portable, raw = _read_json_strict(path, root, 2 * 1024 * 1024)
    except (FileNotFoundError, ForwardError):
        return None, ["portfolio_policy_unreadable"], None
    try:
        errors = portfolio_backtest.validate_policy(document)
    except (KeyError, TypeError, ValueError, OverflowError):
        errors = ["portfolio_policy_malformed_nested_value"]
    return document, errors, raw


def _calendar_semantic_from_sets(holidays: set[str], early: set[str]) -> dict:
    return {
        "schema": "us_equity_market_calendar/1.0",
        "market_timezone": "America/New_York",
        "regular_open": "09:30",
        "regular_close": "16:00",
        "early_close": "13:00",
        "holidays": sorted(holidays),
        "early_closes": sorted(early),
    }


def _calendar_semantic(path: Path) -> dict:
    holidays, early = _calendar(path)
    return _calendar_semantic_from_sets(holidays, early)


def _policy_ref(*, name: str, portable: str, raw: bytes, schema: str,
                version: str, semantic: Any) -> dict:
    return {
        "name": name,
        "path": portable,
        "schema": schema,
        "version": version,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "semantic_sha256": sha256_obj(semantic),
    }


def _policy_contracts(*, policy: dict, policy_path: Path, repository_root: Path,
                      cost_policy_path: Path, portfolio_policy_path: Path,
                      calendar_policy_path: Path) -> dict:
    try:
        policy_snapshot, policy_portable, policy_raw = _read_json_strict(
            policy_path, repository_root, 2 * 1024 * 1024)
        cost, cost_portable, cost_raw = _read_json_strict(
            cost_policy_path, repository_root, 2 * 1024 * 1024)
        portfolio, portfolio_portable, portfolio_raw = _read_json_strict(
            portfolio_policy_path, repository_root, 2 * 1024 * 1024)
        holidays, early, calendar_raw, calendar_portable = _read_calendar(
            calendar_policy_path, repository_root)
    except (FileNotFoundError, ForwardError) as exc:
        raise ForwardError("policy_contract_secure_read_failed") from exc
    if policy_snapshot != policy or validate_policy(policy_snapshot):
        raise ForwardError("allocation_policy_changed_or_invalid")
    try:
        cost_errors = cost_model.validate_policy(cost)
        portfolio_errors = portfolio_backtest.validate_policy(portfolio)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ForwardError("dependent_policy_malformed_nested_value") from exc
    if cost_errors:
        raise ForwardError("cost_policy_invalid")
    if portfolio_errors:
        raise ForwardError("portfolio_policy_invalid")
    calendar_semantic = _calendar_semantic_from_sets(holidays, early)
    return {
        "allocation_policy": _policy_ref(
            name="allocation_policy", portable=policy_portable, raw=policy_raw,
            schema=POLICY_SCHEMA, version=policy_snapshot["version"],
            semantic=policy["allocation_policy"]),
        "cost_policy": _policy_ref(
            name="cost_policy", portable=cost_portable, raw=cost_raw,
            schema=cost_model.POLICY_SCHEMA, version=cost["version"], semantic=cost),
        "portfolio_policy": _policy_ref(
            name="portfolio_policy", portable=portfolio_portable, raw=portfolio_raw,
            schema=portfolio_backtest.POLICY_SCHEMA,
            version=portfolio["version"], semantic=portfolio),
        "calendar_policy": _policy_ref(
            name="calendar_policy", portable=calendar_portable, raw=calendar_raw,
            schema=calendar_semantic["schema"],
            version="1.0.0", semantic=calendar_semantic),
    }


def _validate_expected_run_denominator(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["expected_run_denominator_not_object"]
    errors: list[str] = []
    if set(document) != _EXPECTED_RUN_DENOMINATOR_KEYS:
        errors.append("expected_run_denominator_keys_invalid")
    if document.get("schema") != EXPECTED_RUN_DENOMINATOR_SCHEMA:
        errors.append("expected_run_denominator_schema_invalid")
    for key in ("plan_id", "candidate_strategy_id", "benchmark_strategy_id"):
        if not isinstance(document.get(key), str) or not _ID_RE.fullmatch(
                document.get(key, "")):
            errors.append("expected_run_denominator_id_invalid:" + key)
    if not isinstance(document.get("plan_sha256"), str) or not _SHA_RE.fullmatch(
            document.get("plan_sha256", "")):
        errors.append("expected_run_denominator_plan_hash_invalid")
    incumbents = document.get("incumbent_strategy_ids")
    if (not isinstance(incumbents, list) or not incumbents
            or any(not isinstance(value, str) or not _ID_RE.fullmatch(value)
                   for value in incumbents)
            or incumbents != sorted(set(incumbents))):
        errors.append("expected_run_denominator_incumbents_invalid")
        incumbents = []
    candidate = document.get("candidate_strategy_id")
    benchmark = document.get("benchmark_strategy_id")
    expected = [candidate, benchmark, *incumbents]
    if (any(not isinstance(value, str) for value in expected)
            or len(expected) != len(set(expected))):
        errors.append("expected_run_denominator_strategy_ids_not_unique")
    if document.get("expected_strategy_ids") != expected:
        errors.append("expected_run_denominator_order_or_membership_invalid")
    if document.get("expected_count") != len(expected) or len(expected) < 3:
        errors.append("expected_run_denominator_count_invalid")
    digest = document.get("commitment_sha256")
    if (not isinstance(digest, str) or not _SHA_RE.fullmatch(digest)
            or digest != sha256_obj(_payload(document, "commitment_sha256"))):
        errors.append("expected_run_denominator_hash_mismatch")
    return sorted(set(errors))


def build_expected_run_denominator(*, plan_id: str, plan_sha256: str,
                                   candidate_strategy_id: str,
                                   benchmark_strategy_id: str,
                                   incumbent_strategy_ids: list[str]) -> dict:
    """Build the caller-frozen, non-authoritative expected strategy roster."""
    incumbents = sorted(list(incumbent_strategy_ids))
    document = {
        "schema": EXPECTED_RUN_DENOMINATOR_SCHEMA,
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "candidate_strategy_id": candidate_strategy_id,
        "benchmark_strategy_id": benchmark_strategy_id,
        "incumbent_strategy_ids": incumbents,
        "expected_strategy_ids": [
            candidate_strategy_id, benchmark_strategy_id, *incumbents],
        "expected_count": 2 + len(incumbents),
        "commitment_sha256": "",
    }
    document["commitment_sha256"] = sha256_obj(
        _payload(document, "commitment_sha256"))
    errors = _validate_expected_run_denominator(document)
    if errors:
        raise ForwardError("expected_run_denominator_invalid:" + errors[0])
    return document


def _spec_matches_expected_run_denominator(spec: dict, commitment: dict) -> bool:
    strategy_id = spec.get("strategy_id")
    role = spec.get("role")
    if role == "candidate":
        return strategy_id == commitment.get("candidate_strategy_id")
    if role == "benchmark":
        return strategy_id == commitment.get("benchmark_strategy_id")
    if role == "incumbent":
        return strategy_id in commitment.get("incumbent_strategy_ids", [])
    return False


def build_replay_spec(*, plan_id: str, plan_sha256: str, strategy_id: str, role: str,
                      start_session_date: str, variant_parameters: dict,
                      simulation_assumptions: dict, historical_anchors: dict,
                      expected_run_denominator: dict,
                      created_at_utc: str | None = None,
                      program_id: str = PROGRAM_ID,
                      program_version: str = PROGRAM_VERSION,
                      entry_model: str = "next_open", direction: str = "long",
                      supported_instruments: list[str] | None = None,
                      allocation: dict | None = None,
                      policy_path: Path = POLICY_DEFAULT,
                      cost_policy_path: Path = COST_POLICY_DEFAULT,
                      portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                      calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                      repository_root: Path = ROOT) -> dict:
    policy, errors = load_policy(policy_path, root=repository_root)
    if policy is None or errors:
        raise ForwardError("forward_policy_invalid")
    if (program_id != PROGRAM_ID or program_version != PROGRAM_VERSION
            or entry_model != "next_open" or direction != "long"):
        raise ForwardError("unsupported_strategy_family")
    instruments = list(SUPPORTED_INSTRUMENTS) if supported_instruments is None else list(
        supported_instruments)
    if instruments != list(SUPPORTED_INSTRUMENTS):
        raise ForwardError("supported_instruments_mismatch")
    allocation = copy.deepcopy(allocation) if allocation is not None else {
        key: policy["allocation_policy"][key]
        for key in ("requested_weight", "benchmark_weight", "assumed_fraction_of_adv")
    }
    contracts = _policy_contracts(
        policy=policy, policy_path=policy_path, repository_root=repository_root,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path)
    commitment_errors = _validate_expected_run_denominator(expected_run_denominator)
    if commitment_errors:
        raise ForwardError("expected_run_denominator_invalid:" + commitment_errors[0])
    if (expected_run_denominator.get("plan_id") != plan_id
            or expected_run_denominator.get("plan_sha256") != plan_sha256
            or not _spec_matches_expected_run_denominator(
                {"strategy_id": strategy_id, "role": role}, expected_run_denominator)):
        raise ForwardError("expected_run_denominator_spec_binding_invalid")
    document = {
        "schema": REPLAY_SPEC_SCHEMA,
        "tool": TOOL,
        "algorithm_version": ALGORITHM_VERSION,
        "created_at_utc": _trusted_utc(created_at_utc),
        "start_session_date": start_session_date,
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "strategy_id": strategy_id,
        "role": role,
        "strategy_mode": ("continuous_spy_reference" if role == "benchmark"
                          else "catalyst_event_signals"),
        "program": copy.deepcopy(policy["supported_program"]),
        "variant_parameters": copy.deepcopy(variant_parameters),
        "simulation_assumptions": copy.deepcopy(simulation_assumptions),
        "allocation": allocation,
        "decision_rules": copy.deepcopy(policy["decision_rules"]),
        "eligibility_rules": copy.deepcopy(policy["eligibility_rules"]),
        "simulation_semantics": copy.deepcopy(policy["simulation_semantics"]),
        "policy_contracts": contracts,
        "historical_anchors": copy.deepcopy(historical_anchors),
        "expected_run_denominator": copy.deepcopy(expected_run_denominator),
        "supported_instruments": instruments,
        "safety_constants": copy.deepcopy(SAFETY),
        "replay_spec_sha256": "",
    }
    document["program"]["program_id"] = program_id
    document["program"]["program_version"] = program_version
    document["program"]["entry_model"] = entry_model
    document["program"]["direction"] = direction
    document["replay_spec_sha256"] = sha256_obj(_payload(document, "replay_spec_sha256"))
    validation = validate_replay_spec(
        document, repository_root=repository_root, policy_path=policy_path,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path)
    if validation:
        raise ForwardError("replay_spec_self_validation:" + validation[0])
    return document


def _validate_replay_spec_impl(document: Any, *, repository_root: Path,
                               policy_path: Path, cost_policy_path: Path,
                               portfolio_policy_path: Path,
                               calendar_policy_path: Path,
                               revalidate_contracts: bool) -> list[str]:
    policy, policy_errors = load_policy(policy_path, root=repository_root)
    if policy is None or policy_errors:
        return ["forward_policy_invalid"]
    if not isinstance(document, dict):
        return ["replay_spec_not_object"]
    errors: list[str] = []
    if set(document) != _REPLAY_SPEC_KEYS:
        errors.append("replay_spec_keys_invalid")
    if (document.get("schema") != REPLAY_SPEC_SCHEMA or document.get("tool") != TOOL
            or document.get("algorithm_version") != ALGORITHM_VERSION):
        errors.append("replay_spec_identity_invalid")
    for key in ("plan_id", "strategy_id"):
        if not isinstance(document.get(key), str) or not _ID_RE.fullmatch(document[key]):
            errors.append(f"replay_spec_id_invalid:{key}")
    if not isinstance(document.get("plan_sha256"), str) or not _SHA_RE.fullmatch(
            document.get("plan_sha256", "")):
        errors.append("replay_spec_plan_hash_invalid")
    role = document.get("role")
    if role not in ROLES:
        errors.append("replay_spec_role_invalid")
    expected_mode = "continuous_spy_reference" if role == "benchmark" else "catalyst_event_signals"
    if document.get("strategy_mode") != expected_mode:
        errors.append("replay_spec_strategy_mode_invalid")
    if document.get("program") != policy["supported_program"]:
        errors.append("replay_spec_program_unsupported_or_drifted")
    if document.get("supported_instruments") != list(SUPPORTED_INSTRUMENTS):
        errors.append("replay_spec_supported_instruments_invalid")
    created = _parse_canonical_utc(document.get("created_at_utc"))
    start = _parse_date(document.get("start_session_date"))
    if created is None:
        errors.append("replay_spec_created_at_invalid")
    if start is None:
        errors.append("replay_spec_start_session_invalid")
    else:
        try:
            if not _session_verdict(start, calendar_policy_path)["is_trading_day"]:
                errors.append("replay_spec_start_not_trading_session")
            elif created is not None and created >= _session_open(start, calendar_policy_path):
                errors.append("replay_spec_declared_created_at_not_pre_open")
        except ForwardError as exc:
            errors.append(str(exc))
    parameters = document.get("variant_parameters")
    if not isinstance(parameters, dict) or set(parameters) != _VARIANT_KEYS:
        errors.append("replay_spec_variant_parameters_invalid")
    else:
        bounds = policy["supported_program"]["variant_parameter_bounds"]
        for key in _VARIANT_KEYS - {"max_hold_sessions"}:
            value = parameters.get(key)
            limit = bounds[key]
            if (not _number(value) or not limit["minimum_exclusive"] < float(value)
                    <= limit["maximum_inclusive"]):
                errors.append(f"replay_spec_variant_parameter_invalid:{key}")
        hold = parameters.get("max_hold_sessions")
        limit = bounds["max_hold_sessions"]
        if (not isinstance(hold, int) or isinstance(hold, bool)
                or not limit["minimum_inclusive"] <= hold <= limit["maximum_inclusive"]):
            errors.append("replay_spec_variant_parameter_invalid:max_hold_sessions")
    assumptions = document.get("simulation_assumptions")
    if not isinstance(assumptions, dict) or set(assumptions) != _ASSUMPTION_KEYS:
        errors.append("replay_spec_simulation_assumptions_invalid")
    else:
        for key in _ASSUMPTION_KEYS - {"cost_stress_multiplier"}:
            if not _number(assumptions.get(key), nonnegative=True) or float(
                    assumptions[key]) > 500:
                errors.append(f"replay_spec_assumption_invalid:{key}")
        cost_policy, cost_errors, _cost_raw = _load_cost_policy(
            cost_policy_path, repository_root)
        multiplier = assumptions.get("cost_stress_multiplier")
        if (cost_policy is None or cost_errors or not _number(multiplier)
                or float(multiplier) not in [float(value)
                                             for value in cost_policy["stress_multipliers"]]):
            errors.append("replay_spec_cost_stress_multiplier_invalid")
        else:
            frozen_floor = assumptions.get("transaction_cost_bps_per_side")
            required_post_stress_floor = (
                float(cost_policy["minimum_cost_bps_per_side"]) * float(multiplier))
            if (not _number(frozen_floor, nonnegative=True)
                    or float(frozen_floor) + 1e-12 < required_post_stress_floor):
                errors.append("replay_spec_post_stress_cost_floor_below_policy_minimum")
    allocation = document.get("allocation")
    expected_allocation = {
        key: policy["allocation_policy"][key]
        for key in ("requested_weight", "benchmark_weight", "assumed_fraction_of_adv")
    }
    if not isinstance(allocation, dict) or set(allocation) != _ALLOCATION_KEYS \
            or allocation != expected_allocation:
        errors.append("replay_spec_allocation_invalid")
    for key in ("decision_rules", "eligibility_rules", "simulation_semantics"):
        if document.get(key) != policy[key]:
            errors.append(f"replay_spec_rule_drift:{key}")
    anchors = document.get("historical_anchors")
    if not isinstance(anchors, dict) or set(anchors) != _ANCHOR_KEYS:
        errors.append("replay_spec_historical_anchors_invalid")
    elif any(not isinstance(value, str) or not _SHA_RE.fullmatch(value)
             for value in anchors.values()):
        errors.append("replay_spec_historical_anchor_hash_invalid")
    commitment = document.get("expected_run_denominator")
    commitment_errors = _validate_expected_run_denominator(commitment)
    errors.extend("replay_spec_" + value for value in commitment_errors)
    if (isinstance(commitment, dict)
            and (commitment.get("plan_id") != document.get("plan_id")
                 or commitment.get("plan_sha256") != document.get("plan_sha256"))):
        errors.append("replay_spec_expected_denominator_plan_binding_invalid")
    if isinstance(commitment, dict) and not _spec_matches_expected_run_denominator(
            document, commitment):
        errors.append("replay_spec_expected_denominator_strategy_binding_invalid")
    if document.get("safety_constants") != SAFETY:
        errors.append("replay_spec_safety_invalid")
    contracts = document.get("policy_contracts")
    if not isinstance(contracts, dict) or set(contracts) != _POLICY_CONTRACT_KEYS:
        errors.append("replay_spec_policy_contracts_invalid")
    else:
        for name, ref in contracts.items():
            if not isinstance(ref, dict) or set(ref) != _POLICY_REF_KEYS:
                errors.append(f"replay_spec_policy_ref_shape_invalid:{name}")
                continue
            if ref.get("name") != name:
                errors.append(f"replay_spec_policy_ref_name_invalid:{name}")
            for key in ("file_sha256", "semantic_sha256"):
                if not isinstance(ref.get(key), str) or not _SHA_RE.fullmatch(ref[key]):
                    errors.append(f"replay_spec_policy_ref_hash_invalid:{name}:{key}")
            if not isinstance(ref.get("version"), str) or not _SEMVER_RE.fullmatch(
                    ref.get("version", "")):
                errors.append(f"replay_spec_policy_ref_version_invalid:{name}")
        if revalidate_contracts:
            try:
                expected = _policy_contracts(
                    policy=policy, policy_path=policy_path, repository_root=repository_root,
                    cost_policy_path=cost_policy_path,
                    portfolio_policy_path=portfolio_policy_path,
                    calendar_policy_path=calendar_policy_path)
                if contracts != expected:
                    errors.append("replay_spec_policy_contract_drift")
            except ForwardError:
                errors.append("replay_spec_policy_contract_revalidation_failed")
    if document.get("replay_spec_sha256") != sha256_obj(
            _payload(document, "replay_spec_sha256")):
        errors.append("replay_spec_hash_mismatch")
    return sorted(set(errors))


def validate_replay_spec(document: Any, *, repository_root: Path = ROOT,
                         policy_path: Path = POLICY_DEFAULT,
                         cost_policy_path: Path = COST_POLICY_DEFAULT,
                         portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                         calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                         revalidate_contracts: bool = True) -> list[str]:
    """Fail closed on malformed replay-spec data without leaking conversion errors."""
    try:
        return _validate_replay_spec_impl(
            document, repository_root=repository_root, policy_path=policy_path,
            cost_policy_path=cost_policy_path,
            portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path,
            revalidate_contracts=revalidate_contracts)
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["replay_spec_malformed_nested_value"]


def _input_ref(path: Path, role: str, root: Path, maximum_bytes: int
               ) -> tuple[dict, dict | None]:
    relative, _resolved_root = _relative_under_root(path, root)
    portable = relative.as_posix()
    ref = {
        "role": role,
        "path": portable,
        "status": "missing",
        "file_sha256": None,
        "semantic_sha256": None,
        "schema": None,
        "error": "input_missing",
    }
    try:
        raw, stable_portable = _stable_read_bytes(path, root, maximum_bytes)
    except FileNotFoundError:
        return ref, None
    except ForwardError as exc:
        ref["status"] = "invalid"
        ref["error"] = "secure_input_read_failed:" + str(exc)
        return ref, None
    ref["file_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        document = _strict_json_loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        ref["status"] = "invalid"
        ref["error"] = "input_unreadable_or_malformed"
        return ref, None
    if not isinstance(document, dict):
        ref["status"] = "invalid"
        ref["error"] = "input_json_not_object"
        return ref, None
    ref.update({
        "status": "available",
        "path": stable_portable,
        "semantic_sha256": sha256_obj(document),
        "schema": document.get("schema") if isinstance(document.get("schema"), str) else None,
        "error": None,
    })
    return ref, document


def _manifest(entries: list[dict], source_kind: str, replay_spec: dict,
              previous_included: bool) -> dict:
    document = {
        "source_kind": source_kind,
        "entries": entries,
        "entry_count": len(entries),
        "previous_result_included": previous_included,
        "replay_spec_sha256": replay_spec["replay_spec_sha256"],
        "policy_contracts_sha256": sha256_obj(replay_spec["policy_contracts"]),
        "manifest_sha256": "",
    }
    document["manifest_sha256"] = sha256_obj(_payload(document, "manifest_sha256"))
    return document


def _validate_package_documents(package: Any, documents: dict[str, dict]) -> list[str]:
    """Validate one package entirely from the stable bytes already read by this engine."""
    if not isinstance(package, dict):
        return ["package_not_object"]
    errors: list[str] = []
    for key, expected in feature_corpus.SAFETY.items():
        if package.get(key) != expected:
            errors.append(f"package_safety_invalid:{key}")
    if package.get("schema") != session_capture.PACKAGE_SCHEMA:
        errors.append("package_schema_invalid")
    if package.get("activation_authority") is not False:
        errors.append("package_activation_authority_invalid")
    try:
        if package.get("package_sha256") != session_capture._sha_obj(
                session_capture._payload(package, "package_sha256")):
            errors.append("package_sha256_mismatch")
    except (TypeError, ValueError, OverflowError):
        errors.append("package_hash_uncomputable")
    files = package.get("files")
    if not isinstance(files, dict) or set(files) != feature_corpus.EXPECTED_FILES:
        errors.append("package_files_invalid")
        files = {}
    if set(documents) != feature_corpus.EXPECTED_FILES:
        errors.append("package_member_denominator_invalid")
        return sorted(set(errors))
    for filename, document in documents.items():
        try:
            if files.get(filename) != sha256_obj(document):
                errors.append(f"{filename}:object_hash_mismatch")
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{filename}:hash_uncomputable")

    events = documents["events.json"]
    bars = documents["bars.json"]
    feature_set = documents["features.json"]
    instruments = documents["instruments.json"]
    try:
        errors.extend(f"events:{value}" for value in features.validate_event_snapshot(events))
        errors.extend(f"bars:{value}" for value in features.validate_bars_archive(bars))
        errors.extend(f"features:{value}" for value in features.validate_dataset(feature_set))
    except (KeyError, TypeError, ValueError, OverflowError):
        errors.append("package_member_validation_failed_closed")
        return sorted(set(errors))
    errors.extend("package_" + value for value in _market_bar_bound_errors(bars))
    if errors:
        return sorted(set(errors))
    lineage = feature_set.get("lineage") if isinstance(feature_set, dict) else None
    if not isinstance(lineage, dict):
        errors.append("feature_lineage_missing")
    else:
        if lineage.get("bars_payload_sha256") != sha256_obj(bars):
            errors.append("feature_bars_payload_binding_mismatch")
        if lineage.get("events_payload_sha256") != sha256_obj(events):
            errors.append("feature_events_payload_binding_mismatch")
    if not isinstance(instruments, dict) or any(
            instruments.get(key) != expected for key, expected in features.SAFETY_FIELDS.items()):
        errors.append("instruments_safety_invalid")
        mapping: dict = {}
    else:
        mapping = instruments.get("instrument_types")
        if not isinstance(mapping, dict):
            errors.append("instrument_types_invalid")
            mapping = {}
    event_rows = events.get("events") if isinstance(events, dict) else None
    feature_rows = feature_set.get("rows") if isinstance(feature_set, dict) else None
    if not isinstance(event_rows, list) or not isinstance(feature_rows, list):
        errors.append("package_event_or_feature_rows_invalid")
        return sorted(set(errors))
    event_by_id = {row.get("event_id"): row for row in event_rows if isinstance(row, dict)}
    feature_ids = {row.get("event_id") for row in feature_rows if isinstance(row, dict)}
    if len(event_by_id) != len(event_rows) or feature_ids != set(event_by_id):
        errors.append("event_feature_id_set_mismatch")
    target = package.get("target_session_date")
    if target != instruments.get("target_session_date"):
        errors.append("package_instrument_target_mismatch")
    for event_id, event in event_by_id.items():
        symbol = event.get("symbol")
        if event.get("instrument_type") != mapping.get(symbol):
            errors.append(f"instrument_type_binding_mismatch:{event_id}")
        if event.get("instrument_type") not in SUPPORTED_INSTRUMENTS:
            errors.append(f"unsupported_instrument_type:{event_id}")
    if package.get("event_count") != len(event_by_id):
        errors.append("package_event_count_mismatch")
    eligible = sum(isinstance(row, dict) and row.get("eligible_for_modeling") is True
                   for row in feature_rows)
    if package.get("eligible_feature_row_count") != eligible:
        errors.append("package_eligible_count_mismatch")

    bars_by_symbol = bars.get("bars") if isinstance(bars, dict) else None
    knowledge_cutoff = events.get("knowledge_cutoff_utc") if isinstance(events, dict) else None
    if isinstance(bars_by_symbol, dict) and isinstance(knowledge_cutoff, str):
        try:
            expected_rows = [
                features._build_row(
                    event, bars_by_symbol[event["symbol"]],
                    bars_by_symbol[features.BENCHMARK], knowledge_cutoff)
                for event in sorted(event_rows,
                                    key=lambda value: (value["event_date"], value["event_id"]))
            ]
            if feature_rows != expected_rows:
                errors.append("feature_rows_not_exactly_rederived_from_package_members")
        except (KeyError, TypeError, ValueError, OverflowError):
            errors.append("feature_row_rederivation_failed_closed")
    else:
        errors.append("package_bars_or_knowledge_cutoff_invalid")
    return sorted(set(errors))


def _load_feature_source(path: Path, session: dt.date, input_root: Path,
                         maximum_bytes: int) -> tuple[list[dict], dict[str, str], str,
                                                       list[dict], list[str]]:
    primary_ref, primary = _input_ref(path, "feature_source", input_root, maximum_bytes)
    entries = [primary_ref]
    if primary is None:
        return [], {}, "unavailable", entries, [primary_ref["error"] or "feature_input_invalid"]
    schema = primary.get("schema")
    if schema == feature_corpus.SCHEMA:
        primary_ref["status"] = "invalid"
        primary_ref["error"] = "replaceable_feature_corpus_not_forward_authority"
        return [], {}, "corpus", entries, [primary_ref["error"]]
    if schema == session_capture.PACKAGE_SCHEMA:
        member_docs: dict[str, dict] = {}
        for filename in sorted(feature_corpus.EXPECTED_FILES):
            ref, value = _input_ref(Path(path).parent / filename,
                                    "feature_package_" + filename.removesuffix(".json"),
                                    input_root, maximum_bytes)
            expected_schema = {
                "events.json": features.EVENT_SCHEMA,
                "bars.json": "quant_bars_archive/1.0",
                "features.json": features.DATASET_SCHEMA,
                "instruments.json": "quant_session_instruments/1.0",
            }[filename]
            if value is not None and ref.get("schema") is None:
                ref["schema"] = expected_schema
            elif value is not None and ref.get("schema") != expected_schema:
                ref["status"] = "invalid"
                ref["error"] = "feature_package_member_schema_invalid"
            entries.append(ref)
            if value is not None:
                member_docs[filename] = value
        if any(ref["status"] != "available" for ref in entries):
            return [], {}, "package", entries, [
                "feature_package_member_unavailable_or_invalid"]
        errors = _validate_package_documents(primary, member_docs)
        if errors:
            return [], {}, "package", entries, [
                "feature_package_invalid:" + errors[0]]
        if primary.get("target_session_date") != session.isoformat():
            return [], {}, "package", entries, ["feature_package_session_mismatch"]
        rows = [copy.deepcopy(row) for row in member_docs["features.json"]["rows"]
                if row.get("event_date") == session.isoformat()]
        if len(rows) != len(member_docs["features.json"]["rows"]):
            return [], {}, "package", entries, ["feature_package_contains_other_sessions"]
        events = {row["event_id"]: row for row in member_docs["events.json"]["events"]}
        instruments = {row["event_id"]: events[row["event_id"]]["instrument_type"]
                       for row in rows}
        return rows, instruments, "package", entries, []
    primary_ref["status"] = "invalid"
    primary_ref["error"] = "feature_source_schema_unsupported"
    return [], {}, "unsupported", entries, [primary_ref["error"]]


def _market_bar_bound_errors(document: Any) -> list[str]:
    """Reject economically impossible magnitudes before any derived arithmetic."""
    bars = document.get("bars") if isinstance(document, dict) else None
    if not isinstance(bars, dict):
        return ["market_bar_bound_mapping_invalid"]
    errors: list[str] = []
    for symbol, rows in sorted(bars.items(), key=lambda item: str(item[0])):
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for field in ("open", "high", "low", "close"):
                value = row.get(field)
                if (not _number(value, positive=True)
                        or not MIN_MARKET_PRICE <= float(value) <= MAX_MARKET_PRICE):
                    errors.append(
                        f"market_bar_price_out_of_bounds:{symbol}:{index}:{field}")
    return sorted(set(errors))


def _load_bars(path: Path, session: dt.date, input_root: Path,
               maximum_bytes: int) -> tuple[dict | None, dict, list[str]]:
    ref, document = _input_ref(path, "market_bars", input_root, maximum_bytes)
    if document is None:
        return None, ref, [ref["error"] or "market_bars_invalid"]
    try:
        errors = features.validate_bars_archive(document)
    except (KeyError, TypeError, ValueError, OverflowError):
        errors = ["malformed_nested_value"]
    if errors:
        ref["status"] = "invalid"
        ref["error"] = "market_bars_invalid:" + errors[0]
        return None, ref, [ref["error"]]
    bound_errors = _market_bar_bound_errors(document)
    if bound_errors:
        ref["status"] = "invalid"
        ref["error"] = "market_bars_invalid:" + bound_errors[0]
        return None, ref, [ref["error"]]
    if ref.get("schema") is None:
        ref["schema"] = "quant_bars_archive/1.0"
    elif ref["schema"] != "quant_bars_archive/1.0":
        ref["status"] = "invalid"
        ref["error"] = "market_bars_schema_invalid"
        return None, ref, [ref["error"]]
    if any(row.get("date", "") > session.isoformat()
           for rows in document["bars"].values() for row in rows):
        ref["status"] = "invalid"
        ref["error"] = "market_bars_contains_future_session"
        return None, ref, [ref["error"]]
    return document, ref, []


def _feature_bar_boundary_errors(rows: list[dict], bars: dict, session: dt.date,
                                 calendar_path: Path) -> list[str]:
    """Bind every signal row to pre-open knowledge and the exact prior-bar prefix."""
    errors: list[str] = []
    try:
        market_open = _session_open(session, calendar_path)
        prior_session = _previous_session(session, calendar_path).isoformat()
    except ForwardError as exc:
        return ["feature_boundary_calendar_invalid:" + str(exc)]
    bars_by_symbol = bars.get("bars") if isinstance(bars, dict) else None
    if not isinstance(bars_by_symbol, dict):
        return ["feature_boundary_bars_invalid"]
    benchmark_rows = bars_by_symbol.get(features.BENCHMARK)
    if not isinstance(benchmark_rows, list):
        return ["feature_boundary_benchmark_missing"]
    benchmark_prior = [value for value in benchmark_rows
                       if isinstance(value, dict) and value.get("date", "") < session.isoformat()]
    for index, row in enumerate(rows):
        event_id = row.get("event_id") if isinstance(row, dict) else None
        label = event_id if isinstance(event_id, str) else str(index)
        if not isinstance(row, dict) or row.get("event_date") != session.isoformat():
            errors.append(f"feature_event_session_mismatch:{label}")
            continue
        observed = _parse_canonical_utc(row.get("event_observed_at_utc"))
        knowledge = _parse_canonical_utc(row.get("knowledge_cutoff_utc"))
        if (observed is None or knowledge is None or observed > knowledge
                or observed >= market_open or knowledge >= market_open):
            errors.append(f"feature_point_in_time_boundary_invalid:{label}")
        symbol = row.get("symbol")
        symbol_rows = bars_by_symbol.get(symbol) if isinstance(symbol, str) else None
        symbol_prior = ([value for value in symbol_rows
                         if isinstance(value, dict)
                         and value.get("date", "") < session.isoformat()]
                        if isinstance(symbol_rows, list) else [])
        if (not symbol_prior or not benchmark_prior
                or symbol_prior[-1].get("date") != prior_session
                or benchmark_prior[-1].get("date") != prior_session
                or row.get("usable_bar_cutoff") != prior_session
                or row.get("benchmark_bar_cutoff") != prior_session):
            errors.append(f"feature_prior_session_binding_invalid:{label}")
            continue
        values = row.get("features") if isinstance(row.get("features"), dict) else {}
        availability = (row.get("feature_availability")
                        if isinstance(row.get("feature_availability"), dict) else {})
        prior_state = availability.get("prior_close")
        prior_close = values.get("prior_close")
        if (not _number(prior_close, positive=True)
                or not _number(symbol_prior[-1].get("close"), positive=True)
                or float(prior_close) != float(symbol_prior[-1]["close"])
                or not isinstance(prior_state, dict)
                or prior_state.get("available") is not True
                or prior_state.get("reason") is not None
                or prior_state.get("source") != "daily_bar_prior_close"):
            errors.append(f"feature_prior_close_binding_invalid:{label}")
        lineage = row.get("prior_bar_lineage")
        if (not isinstance(lineage, dict)
                or lineage.get("contract") !=
                    "exact_ordered_bars_with_date_strictly_before_event"
                or lineage.get("symbol_bars_sha256") != features._sha256_obj(symbol_prior)
                or lineage.get("benchmark_bars_sha256") !=
                    features._sha256_obj(benchmark_prior)):
            errors.append(f"feature_prior_bar_lineage_mismatch:{label}")
    return sorted(set(errors))


def _position_payload(position: dict) -> dict:
    return _payload(position, "position_sha256")


def _seal_position(position: dict) -> dict:
    position["position_sha256"] = sha256_obj(_position_payload(position))
    return position


def _state(*, as_of: str | None, data_status: str, cash: float,
           positions: list[dict], cumulative_cost: float) -> dict:
    ordered = sorted((copy.deepcopy(position) for position in positions),
                     key=lambda value: value["position_id"])
    gross = sum(float(position["market_value"]) for position in ordered)
    nav = float(cash) + gross
    if nav <= 0 or cash < -1e-10:
        raise ForwardError("state_nav_or_cash_invalid")
    risk = sum(float(position["planned_stop_risk"]) for position in ordered)
    document = {
        "as_of_session_date": as_of,
        "state_data_status": data_status,
        "normalized_nav": _round(nav),
        "cash": _round(max(0.0, cash)),
        "gross_market_value": _round(gross),
        "gross_exposure": _round(gross / nav),
        "net_exposure": _round(gross / nav),
        "aggregate_stop_risk": _round(risk / nav),
        "cumulative_modeled_cost": _round(cumulative_cost),
        "open_position_count": len(ordered),
        "open_positions": ordered,
        "state_sha256": "",
    }
    document["state_sha256"] = sha256_obj(_payload(document, "state_sha256"))
    return document


def _genesis_state() -> dict:
    return _state(as_of=None, data_status="genesis", cash=1.0, positions=[],
                  cumulative_cost=0.0)


def _carry_unavailable(opening: dict, session: str) -> dict:
    return _state(as_of=session, data_status="unavailable_carried_without_mark",
                  cash=float(opening["cash"]), positions=opening["open_positions"],
                  cumulative_cost=float(opening["cumulative_modeled_cost"]))


def _nav_rows(opening: dict, terminal: dict, session: str, *, status: str,
              entry_cost: float, exit_cost: float) -> tuple[list[dict], float | None]:
    recurrence = float(terminal["normalized_nav"]) / float(opening["normalized_nav"]) - 1.0
    net_return = _round(recurrence) if status == "complete" else None
    rows = [
        {
            "point": "opening", "session_date": session,
            "normalized_nav": opening["normalized_nav"], "cash": opening["cash"],
            "gross_market_value": opening["gross_market_value"],
            "gross_exposure": opening["gross_exposure"],
            "state_sha256": opening["state_sha256"], "session_return": None,
            "return_available": False, "entry_cost": 0.0, "exit_cost": 0.0,
            "row_sha256": "",
        },
        {
            "point": "terminal", "session_date": session,
            "normalized_nav": terminal["normalized_nav"], "cash": terminal["cash"],
            "gross_market_value": terminal["gross_market_value"],
            "gross_exposure": terminal["gross_exposure"],
            "state_sha256": terminal["state_sha256"], "session_return": net_return,
            "return_available": status == "complete", "entry_cost": _round(entry_cost),
            "exit_cost": _round(exit_cost), "row_sha256": "",
        },
    ]
    for row in rows:
        row["row_sha256"] = sha256_obj(_payload(row, "row_sha256"))
    return rows, net_return


def _unavailable_denominator() -> dict:
    document = {
        "source_status": "unavailable", "source_row_count": 0,
        "eligible_row_count": 0, "ineligible_row_count": 0,
        "generated_signal_count": 0, "entered_signal_count": 0,
        "excluded_signal_count": 0, "decision_count": 0,
        "source_event_ids": [], "zero_signal_session": None,
        "denominator_sha256": "",
    }
    document["denominator_sha256"] = sha256_obj(
        _payload(document, "denominator_sha256"))
    return document


def _complete_denominator(rows: list[dict], decisions: list[dict]) -> dict:
    eligible = sum(decision["eligible_signal"] for decision in decisions)
    entered = sum(decision["status"] == "entered" for decision in decisions)
    document = {
        "source_status": "available", "source_row_count": len(rows),
        "eligible_row_count": sum(row.get("eligible_for_modeling") is True for row in rows),
        "ineligible_row_count": sum(row.get("eligible_for_modeling") is not True for row in rows),
        "generated_signal_count": eligible, "entered_signal_count": entered,
        "excluded_signal_count": len(rows) - entered, "decision_count": len(decisions),
        "source_event_ids": [row["event_id"] for row in rows],
        "zero_signal_session": eligible == 0, "denominator_sha256": "",
    }
    document["denominator_sha256"] = sha256_obj(
        _payload(document, "denominator_sha256"))
    return document


def _decision_base(row: dict, instrument: str, priority: int) -> dict:
    return {
        "decision_id": "decision:" + row["event_id"],
        "event_id": row["event_id"], "symbol": row["symbol"],
        "instrument_type": instrument, "feature_row_sha256": row["row_sha256"],
        "priority": priority, "eligible_signal": False, "status": "ineligible",
        "reason_codes": [],
        "levels": {key: None for key in _LEVEL_KEYS},
        "cost": {
            "status": "not_evaluated", "estimate_sha256": None,
            "estimated_bps_per_side": None, "frozen_floor_bps_per_side": None,
            "applied_bps_per_side": None,
        },
        "allocated_weight": None, "position_id": None, "decision_sha256": "",
    }


def _seal_decision(decision: dict) -> dict:
    decision["reason_codes"] = sorted(set(decision["reason_codes"]))
    decision["decision_sha256"] = sha256_obj(_payload(decision, "decision_sha256"))
    return decision


def _bar_for(archive: dict, symbol: str, session: str) -> dict | None:
    matches = [row for row in archive.get("bars", {}).get(symbol, [])
               if row.get("date") == session]
    return matches[0] if len(matches) == 1 else None


def _position_event(position: dict, *, entered: bool, action: str,
                    exit_reason: str | None, exit_price: float | None,
                    mark_price: float | None, gross_value: float,
                    exit_cost: float, held_sessions: int) -> dict:
    event = {
        "position_id": position["position_id"], "event_id": position["event_id"],
        "symbol": position["symbol"], "was_entered_this_session": entered,
        "action": action, "exit_reason": exit_reason, "exit_price": _round(exit_price),
        "mark_price": _round(mark_price), "gross_value": _round(gross_value),
        "exit_cost": _round(exit_cost), "held_sessions": held_sessions,
        "event_sha256": "",
    }
    event["event_sha256"] = sha256_obj(_payload(event, "event_sha256"))
    return event


def _benchmark_denominator() -> dict:
    document = {
        "source_status": "not_applicable", "source_row_count": 0,
        "eligible_row_count": 0, "ineligible_row_count": 0,
        "generated_signal_count": 0, "entered_signal_count": 0,
        "excluded_signal_count": 0, "decision_count": 0,
        "source_event_ids": [], "zero_signal_session": True,
        "denominator_sha256": "",
    }
    document["denominator_sha256"] = sha256_obj(
        _payload(document, "denominator_sha256"))
    return document


def _prepare_decisions(rows: list[dict], instruments: dict[str, str], replay_spec: dict
                       ) -> list[dict]:
    params = replay_spec["variant_parameters"]
    decisions: list[dict] = []
    for priority, row in enumerate(sorted(rows, key=lambda value: value["event_id"]), start=1):
        instrument = instruments.get(row["event_id"])
        decision = _decision_base(row, instrument, priority)
        exclusions = row.get("exclusion_reasons")
        if row.get("eligible_for_modeling") is not True:
            decision["reason_codes"] = ["feature_row_ineligible"] + (
                list(exclusions) if isinstance(exclusions, list) else [])
            decisions.append(_seal_decision(decision))
            continue
        if exclusions != []:
            decision["reason_codes"] = ["feature_exclusion_denominator_inconsistent"]
            decisions.append(_seal_decision(decision))
            continue
        if instrument not in SUPPORTED_INSTRUMENTS:
            decision["reason_codes"] = ["instrument_unsupported"]
            decisions.append(_seal_decision(decision))
            continue
        values = row.get("features") if isinstance(row.get("features"), dict) else {}
        prior, atr = values.get("prior_close"), values.get("atr_14")
        if not _number(prior, positive=True) or not _number(atr, positive=True):
            decision["reason_codes"] = ["required_signal_feature_missing"]
            decisions.append(_seal_decision(decision))
            continue
        maximum = float(prior) + float(params["max_entry_gap_atr"]) * float(atr)
        stop = float(prior) - float(params["stop_atr"]) * float(atr)
        target = maximum + float(params["target_r_multiple"]) * (maximum - stop)
        decision["levels"].update({
            "prior_close": _round(prior), "atr_14": _round(atr),
            "max_entry": _round(maximum), "stop": _round(stop), "target": _round(target),
        })
        if not 0 < stop < maximum < target:
            decision["reason_codes"] = ["derived_levels_invalid"]
            decisions.append(_seal_decision(decision))
            continue
        decision["eligible_signal"] = True
        decision["status"] = "not_entered"
        decision["reason_codes"] = ["pending_entry_evaluation"]
        decisions.append(_seal_decision(decision))
    return decisions


def _cost_for_decision(decision: dict, row: dict, replay_spec: dict,
                       cost_policy: dict) -> tuple[dict, float | None]:
    values = row["features"]
    annual = values.get("volatility_20_annualized")
    daily_bps = None
    if _number(annual, positive=True):
        daily_bps = float(annual) * 10000.0 / math.sqrt(252.0)
    context = {
        "observed_at_utc": row["knowledge_cutoff_utc"],
        "decision_cutoff_utc": row["knowledge_cutoff_utc"],
        "spread_bps": values.get("spread_bps") if _number(
            values.get("spread_bps"), positive=True) else None,
        "daily_volatility_bps": daily_bps,
        "fraction_of_adv": replay_spec["allocation"]["assumed_fraction_of_adv"],
        "source_artifact_sha256": row["row_sha256"],
    }
    assumptions = replay_spec["simulation_assumptions"]
    estimate = cost_model.estimate(
        context, cost_policy, float(assumptions["cost_stress_multiplier"]))
    # This frozen assumption is explicitly a post-stress floor.  The estimate
    # applies the selected multiplier internally, exactly once.
    frozen = float(assumptions["transaction_cost_bps_per_side"])
    if not estimate.get("ok") or estimate.get("status") != "estimated":
        cost = {
            "status": estimate.get("status", "invalid"),
            "estimate_sha256": estimate.get("estimate_sha256"),
            "estimated_bps_per_side": estimate.get("modeled_cost_bps_per_side"),
            "frozen_floor_bps_per_side": _round(frozen),
            "applied_bps_per_side": None,
        }
        return cost, None
    modeled = float(estimate["modeled_cost_bps_per_side"])
    applied = max(frozen, modeled)
    return {
        "status": "estimated", "estimate_sha256": estimate["estimate_sha256"],
        "estimated_bps_per_side": _round(modeled),
        "frozen_floor_bps_per_side": _round(frozen),
        "applied_bps_per_side": _round(applied),
    }, applied


def _candidate_transition(*, opening: dict, rows: list[dict], instruments: dict[str, str],
                          bars: dict, session: str, replay_spec: dict,
                          cost_policy: dict, portfolio_policy: dict
                          ) -> tuple[dict, list[dict], list[dict], float, float, int, int]:
    decisions = _prepare_decisions(rows, instruments, replay_spec)
    row_by_id = {row["event_id"]: row for row in rows}
    positions = {position["position_id"]: copy.deepcopy(position)
                 for position in opening["open_positions"]}
    cash = float(opening["cash"])
    opening_nav = float(opening["normalized_nav"])
    current_gross = sum(float(position["market_value"]) for position in positions.values())
    current_risk = sum(float(position["planned_stop_risk"]) for position in positions.values())
    sectors: dict[str, float] = {}
    catalysts: dict[str, float] = {}
    for position in positions.values():
        sectors[position["sector"]] = sectors.get(position["sector"], 0.0) + float(
            position["market_value"])
        catalysts[position["catalyst_type"]] = catalysts.get(
            position["catalyst_type"], 0.0) + float(position["market_value"])
    assumptions = replay_spec["simulation_assumptions"]
    entry_cost_total = 0.0
    entry_count = 0
    entered_ids: set[str] = set()
    final_decisions: list[dict] = []
    for decision in decisions:
        decision = copy.deepcopy(decision)
        if not decision["eligible_signal"]:
            final_decisions.append(decision)
            continue
        row = row_by_id[decision["event_id"]]
        bar = _bar_for(bars, decision["symbol"], session)
        if bar is None:
            raise ForwardError("eligible_signal_market_bar_missing:" + decision["event_id"])
        entry = float(bar["open"]) * (
            1.0 + float(assumptions["entry_slippage_bps"]) / 10000.0)
        decision["levels"]["modeled_entry"] = _round(entry)
        decision["reason_codes"] = []
        if entry > float(decision["levels"]["max_entry"]):
            decision["reason_codes"] = ["modeled_entry_above_frozen_max_entry"]
            final_decisions.append(_seal_decision(decision))
            continue
        if not float(decision["levels"]["stop"]) < entry < float(
                decision["levels"]["target"]):
            decision["reason_codes"] = ["realized_levels_invalid"]
            final_decisions.append(_seal_decision(decision))
            continue
        cost, applied_bps = _cost_for_decision(decision, row, replay_spec, cost_policy)
        decision["cost"] = cost
        if applied_bps is None:
            decision["reason_codes"] = ["predecision_cost_unavailable"]
            final_decisions.append(_seal_decision(decision))
            continue
        hard_reason = None
        if entry_count >= portfolio_policy["maximum_entries_per_session"]:
            hard_reason = "maximum_entries_per_session"
        elif len(positions) >= portfolio_policy["maximum_open_positions"]:
            hard_reason = "maximum_open_positions"
        elif portfolio_policy["one_position_per_symbol"] and any(
                position["symbol"] == decision["symbol"] for position in positions.values()):
            hard_reason = "symbol_already_open"
        if hard_reason:
            decision["reason_codes"] = [hard_reason]
            final_decisions.append(_seal_decision(decision))
            continue
        cost_rate = float(applied_bps) / 10000.0
        stop = float(decision["levels"]["stop"])
        modeled_stop_fill = stop * (
            1.0 - float(assumptions["stop_slippage_bps"]) / 10000.0)
        unit_risk = (entry - modeled_stop_fill) / entry + 2.0 * cost_rate
        sector = "unknown_sector"
        catalyst = row["features"].get("catalyst_type")
        if not isinstance(catalyst, str) or not catalyst:
            catalyst = "unknown_catalyst"
        requested = float(replay_spec["allocation"]["requested_weight"]) * opening_nav
        caps = {
            "position": float(portfolio_policy["maximum_position_weight"]) * opening_nav,
            "gross": max(0.0, float(portfolio_policy["maximum_gross_exposure"])
                         * opening_nav - current_gross),
            "net": max(0.0, float(portfolio_policy["maximum_net_exposure"])
                       * opening_nav - current_gross),
            "sector": max(0.0, float(portfolio_policy["maximum_sector_weight"])
                          * opening_nav - sectors.get(sector, 0.0)),
            "catalyst": max(0.0, float(portfolio_policy["maximum_catalyst_weight"])
                            * opening_nav - catalysts.get(catalyst, 0.0)),
            "stop_risk": max(0.0, (float(portfolio_policy["maximum_aggregate_stop_risk"])
                                    * opening_nav - current_risk) / unit_risk),
            "cash": max(0.0, cash / (1.0 + cost_rate)),
        }
        notional = min([requested, *caps.values()])
        minimum = float(portfolio_policy["minimum_position_weight"]) * opening_nav
        if notional + 1e-12 < minimum:
            decision["reason_codes"] = ["constrained_below_minimum_position_weight"]
            final_decisions.append(_seal_decision(decision))
            continue
        entry_cost = notional * cost_rate
        units = notional / entry
        cash -= notional + entry_cost
        if cash < -1e-10:
            raise ForwardError("negative_cash_after_modeled_entry")
        cash = max(0.0, cash)
        position_id = "position:" + sha256_obj({
            "strategy_id": replay_spec["strategy_id"], "event_id": decision["event_id"],
            "session": session, "spec": replay_spec["replay_spec_sha256"],
        })[:24]
        position = _seal_position({
            "position_id": position_id, "position_kind": "catalyst_signal",
            "event_id": decision["event_id"], "symbol": decision["symbol"],
            "instrument_type": decision["instrument_type"], "sector": sector,
            "catalyst_type": catalyst, "entry_session_date": session,
            "entry_price": _round(entry), "entry_notional": _round(notional),
            "entry_cost": _round(entry_cost), "cost_rate_per_side": _round(cost_rate),
            "max_entry": decision["levels"]["max_entry"],
            "stop_price": decision["levels"]["stop"],
            "target_price": decision["levels"]["target"], "units": _round(units),
            "last_mark_price": _round(entry), "market_value": _round(notional),
            "held_sessions": 0,
            "max_hold_sessions": replay_spec["variant_parameters"]["max_hold_sessions"],
            "planned_stop_risk": _round(notional * unit_risk),
            "position_sha256": "",
        })
        positions[position_id] = position
        entered_ids.add(position_id)
        entry_count += 1
        entry_cost_total += entry_cost
        current_gross += notional
        current_risk += notional * unit_risk
        sectors[sector] = sectors.get(sector, 0.0) + notional
        catalysts[catalyst] = catalysts.get(catalyst, 0.0) + notional
        decision["status"] = "entered"
        decision["reason_codes"] = ["passed_entry_constraints"]
        decision["allocated_weight"] = _round(notional / opening_nav)
        decision["position_id"] = position_id
        final_decisions.append(_seal_decision(decision))

    exit_cost_total = 0.0
    exit_count = 0
    position_events: list[dict] = []
    remaining: list[dict] = []
    for position_id in sorted(positions):
        position = positions[position_id]
        bar = _bar_for(bars, position["symbol"], session)
        if bar is None:
            raise ForwardError("open_position_market_bar_missing:" + position["symbol"])
        entered = position_id in entered_ids
        stop = float(position["stop_price"])
        target = float(position["target_price"])
        open_price, low, high = float(bar["open"]), float(bar["low"]), float(bar["high"])
        held = int(position["held_sessions"]) + 1
        exit_price = None
        reason = None
        if open_price <= stop:
            exit_price = open_price * (1.0 - float(assumptions["stop_slippage_bps"]) / 10000.0)
            reason = "gap_through_stop"
        elif low <= stop:
            exit_price = stop * (1.0 - float(assumptions["stop_slippage_bps"]) / 10000.0)
            reason = "stop_first_same_bar_ambiguity" if high >= target else "stop"
        elif open_price >= target or high >= target:
            exit_price = target * (1.0 - float(assumptions["exit_slippage_bps"]) / 10000.0)
            reason = "target"
        elif held >= int(position["max_hold_sessions"]):
            exit_price = float(bar["close"]) * (
                1.0 - float(assumptions["exit_slippage_bps"]) / 10000.0)
            reason = "time_exit"
        if exit_price is not None:
            gross = float(position["units"]) * exit_price
            exit_cost = gross * float(position["cost_rate_per_side"])
            cash += gross - exit_cost
            exit_cost_total += exit_cost
            exit_count += 1
            position_events.append(_position_event(
                position, entered=entered, action="exited", exit_reason=reason,
                exit_price=exit_price, mark_price=None, gross_value=gross,
                exit_cost=exit_cost, held_sessions=held))
        else:
            mark = float(bar["close"])
            updated = copy.deepcopy(position)
            updated["last_mark_price"] = _round(mark)
            updated["market_value"] = _round(float(updated["units"]) * mark)
            updated["held_sessions"] = held
            remaining.append(_seal_position(updated))
            position_events.append(_position_event(
                updated, entered=entered, action="held", exit_reason=None,
                exit_price=None, mark_price=mark, gross_value=float(updated["market_value"]),
                exit_cost=0.0, held_sessions=held))
    terminal = _state(
        as_of=session, data_status="complete", cash=cash, positions=remaining,
        cumulative_cost=float(opening["cumulative_modeled_cost"])
        + entry_cost_total + exit_cost_total)
    return (terminal, final_decisions, position_events, entry_cost_total, exit_cost_total,
            entry_count, exit_count)


def _benchmark_transition(*, opening: dict, bars: dict, session: str, replay_spec: dict,
                          cost_policy: dict
                          ) -> tuple[dict, list[dict], float, float, int, int]:
    bar = _bar_for(bars, "SPY", session)
    if bar is None:
        raise ForwardError("benchmark_spy_bar_missing")
    positions = [copy.deepcopy(value) for value in opening["open_positions"]]
    if len(positions) > 1 or (positions and (
            positions[0]["position_kind"] != "continuous_spy"
            or positions[0]["symbol"] != "SPY")):
        raise ForwardError("benchmark_opening_state_invalid")
    cash = float(opening["cash"])
    assumptions = replay_spec["simulation_assumptions"]
    entry_cost_total = 0.0
    entry_count = 0
    entered = False
    if not positions:
        # There is no pre-decision spread/impact context for the continuous benchmark.
        # Use the same validated post-stress floor as the catalyst path.  The max is
        # defensive and should be equal because replay-spec validation enforces it.
        multiplier = float(assumptions["cost_stress_multiplier"])
        post_stress_minimum = (
            float(cost_policy["minimum_cost_bps_per_side"]) * multiplier)
        cost_bps = max(float(assumptions["transaction_cost_bps_per_side"]),
                       post_stress_minimum)
        cost_rate = cost_bps / 10000.0
        entry = float(bar["open"]) * (
            1.0 + float(assumptions["entry_slippage_bps"]) / 10000.0)
        requested = (float(replay_spec["allocation"]["benchmark_weight"])
                     * float(opening["normalized_nav"]))
        notional = min(requested, cash / (1.0 + cost_rate))
        if notional <= 0:
            raise ForwardError("benchmark_entry_notional_invalid")
        entry_cost_total = notional * cost_rate
        cash -= notional + entry_cost_total
        units = notional / entry
        position = _seal_position({
            "position_id": "position:benchmark-spy", "position_kind": "continuous_spy",
            "event_id": "benchmark:SPY", "symbol": "SPY",
            "instrument_type": "unleveraged_etf", "sector": "benchmark",
            "catalyst_type": "not_applicable", "entry_session_date": session,
            "entry_price": _round(entry), "entry_notional": _round(notional),
            "entry_cost": _round(entry_cost_total), "cost_rate_per_side": _round(cost_rate),
            "max_entry": None, "stop_price": None, "target_price": None,
            "units": _round(units), "last_mark_price": _round(entry),
            "market_value": _round(notional), "held_sessions": 0,
            "max_hold_sessions": None, "planned_stop_risk": 0.0,
            "position_sha256": "",
        })
        positions = [position]
        entry_count = 1
        entered = True
    position = positions[0]
    mark = float(bar["close"])
    position["last_mark_price"] = _round(mark)
    position["market_value"] = _round(float(position["units"]) * mark)
    position["held_sessions"] = int(position["held_sessions"]) + 1
    position = _seal_position(position)
    event = _position_event(
        position, entered=entered, action="held", exit_reason=None, exit_price=None,
        mark_price=mark, gross_value=float(position["market_value"]), exit_cost=0.0,
        held_sessions=position["held_sessions"])
    terminal = _state(
        as_of=session, data_status="complete", cash=cash, positions=[position],
        cumulative_cost=float(opening["cumulative_modeled_cost"]) + entry_cost_total)
    return terminal, [event], entry_cost_total, 0.0, entry_count, 0


def _load_previous(path: Path, replay_spec: dict, session: dt.date, input_root: Path,
                   maximum_bytes: int, calendar_path: Path,
                   chain_paths: frozenset[str], chain_depth: int,
                   policy_path: Path, cost_policy_path: Path,
                   portfolio_policy_path: Path, repository_root: Path
                   ) -> tuple[dict, dict, list[str]]:
    ref, previous = _input_ref(path, "previous_result", input_root, maximum_bytes)
    if previous is None:
        raise ForwardError("previous_result_unavailable")
    if chain_depth >= MAX_CHAIN_LENGTH:
        raise ForwardError("previous_result_chain_too_long")
    if ref["path"] in chain_paths:
        raise ForwardError("previous_result_chain_cycle")
    next_paths = chain_paths | {ref["path"]}
    validation = validate_forward_result(
        previous, replay_spec=replay_spec, input_root=input_root,
        policy_path=policy_path, cost_policy_path=cost_policy_path,
        portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_path, repository_root=repository_root,
        revalidate_inputs=True, previous_result=None,
        _chain_paths=next_paths, _chain_depth=chain_depth + 1)
    if validation:
        raise ForwardError("previous_result_invalid:" + validation[0])
    if (previous.get("plan_id") != replay_spec["plan_id"]
            or previous.get("plan_sha256") != replay_spec["plan_sha256"]
            or previous.get("strategy_id") != replay_spec["strategy_id"]
            or previous.get("role") != replay_spec["role"]
            or previous.get("replay_spec_sha256") != replay_spec["replay_spec_sha256"]):
        raise ForwardError("previous_result_identity_mismatch")
    previous_date = _parse_date(previous.get("session_date"))
    if previous_date is None or _next_session(previous_date, calendar_path) != session:
        raise ForwardError("previous_result_session_not_contiguous")
    if ref["semantic_sha256"] != sha256_obj(previous):
        raise ForwardError("previous_result_manifest_hash_mismatch")
    return previous, ref, []


def _assemble_result(*, replay_spec: dict, session: str, close_utc: str,
                     generated_at_utc: str, data_cutoff_utc: str, status: str,
                     reason_codes: list[str], previous: dict | None, manifest: dict,
                     denominator: dict, decisions: list[dict], events: list[dict],
                     opening: dict, terminal: dict, entry_cost: float, exit_cost: float,
                     entry_count: int, exit_count: int) -> dict:
    nav_rows, net_return = _nav_rows(
        opening, terminal, session, status=status, entry_cost=entry_cost, exit_cost=exit_cost)
    document = {
        **SAFETY,
        "schema": FORWARD_RESULT_SCHEMA,
        "tool": TOOL,
        "algorithm_version": ALGORITHM_VERSION,
        "artifact_valid": True,
        "plan_id": replay_spec["plan_id"],
        "plan_sha256": replay_spec["plan_sha256"],
        "strategy_id": replay_spec["strategy_id"],
        "role": replay_spec["role"],
        "session_date": session,
        "session_close_utc": close_utc,
        "generated_at_utc": generated_at_utc,
        "data_cutoff_utc": data_cutoff_utc,
        "status": status,
        "reason_codes": sorted(set(reason_codes)),
        "replay_spec_sha256": replay_spec["replay_spec_sha256"],
        "previous_result_sha256": previous["result_sha256"] if previous else None,
        "previous_terminal_state_sha256": (
            previous["terminal_state"]["state_sha256"] if previous else None),
        "input_manifest": manifest,
        "signal_denominator": denominator,
        "decisions": decisions,
        "position_events": events,
        "opening_state": opening,
        "terminal_state": terminal,
        "nav_rows": nav_rows,
        "net_return": net_return,
        "gross_exposure": terminal["gross_exposure"],
        "active": float(terminal["gross_exposure"]) > 1e-12,
        "modeled_costs": {
            "entry_cost": _round(entry_cost), "exit_cost": _round(exit_cost),
            "total_session_cost": _round(entry_cost + exit_cost),
            "cumulative_modeled_cost": terminal["cumulative_modeled_cost"],
            "entry_count": entry_count, "exit_count": exit_count,
        },
        "limitations": list(LIMITATIONS),
        "result_sha256": "",
    }
    document["result_sha256"] = sha256_obj(_payload(document, "result_sha256"))
    return document


def _build_forward_result_impl(*, replay_spec: dict, session_date: str,
                               feature_input_path: Path, bars_path: Path,
                               previous_result_path: Path | None,
                               generated_at_utc: str | None, data_cutoff_utc: str | None,
                               input_root: Path, policy_path: Path,
                               cost_policy_path: Path, portfolio_policy_path: Path,
                               calendar_policy_path: Path, repository_root: Path,
                               validate_output: bool,
                               chain_paths: frozenset[str] = frozenset(),
                               chain_depth: int = 0) -> dict:
    spec_errors = validate_replay_spec(
        replay_spec, repository_root=repository_root, policy_path=policy_path,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path)
    if spec_errors:
        raise ForwardError("replay_spec_invalid:" + spec_errors[0])
    policy, policy_errors = load_policy(policy_path, root=repository_root)
    cost_policy, cost_errors, _cost_raw = _load_cost_policy(
        cost_policy_path, repository_root)
    portfolio_policy, portfolio_errors, _portfolio_raw = _load_portfolio_policy(
        portfolio_policy_path, repository_root)
    if policy is None or policy_errors:
        raise ForwardError("forward_policy_invalid")
    if cost_policy is None or cost_errors:
        raise ForwardError("cost_policy_invalid")
    if portfolio_policy is None or portfolio_errors:
        raise ForwardError("portfolio_policy_invalid")
    session = _parse_date(session_date)
    start = _parse_date(replay_spec["start_session_date"])
    if session is None or start is None or session < start:
        raise ForwardError("forward_session_invalid_or_before_start")
    verdict = _session_verdict(session, calendar_policy_path)
    if not verdict["is_trading_day"]:
        raise ForwardError("forward_session_not_trading_day")
    close = _session_close(session, calendar_policy_path)
    generated_text = _trusted_utc(generated_at_utc)
    generated = _parse_utc(generated_text)
    cutoff_text = _trusted_utc(data_cutoff_utc or generated_text)
    cutoff = _parse_utc(cutoff_text)
    if generated is None or cutoff is None or cutoff < close or generated < cutoff:
        raise ForwardError("forward_timestamp_or_cutoff_invalid")
    maximum_bytes = policy["path_policy"]["maximum_input_bytes"]
    previous = None
    previous_ref = None
    if session == start:
        if previous_result_path is not None:
            raise ForwardError("genesis_previous_result_forbidden")
        opening = _genesis_state()
    else:
        if previous_result_path is None:
            raise ForwardError("previous_result_required_after_genesis")
        previous, previous_ref, _ = _load_previous(
            previous_result_path, replay_spec, session, input_root, maximum_bytes,
            calendar_policy_path, chain_paths, chain_depth, policy_path,
            cost_policy_path, portfolio_policy_path, repository_root)
        opening = copy.deepcopy(previous["terminal_state"])

    rows, instruments, source_kind, source_entries, source_errors = _load_feature_source(
        feature_input_path, session, input_root, maximum_bytes)
    bars, bars_ref, bars_errors = _load_bars(
        bars_path, session, input_root, maximum_bytes)
    boundary_errors = (_feature_bar_boundary_errors(
        rows, bars, session, calendar_policy_path)
        if source_kind == "package" and bars is not None else [])
    entries = [*source_entries, bars_ref]
    if previous_ref is not None:
        entries.append(previous_ref)
    manifest = _manifest(
        entries, source_kind, replay_spec, previous_included=previous_ref is not None)
    reason_codes = [*bars_errors]
    if replay_spec["role"] != "benchmark":
        reason_codes.extend([*source_errors, *boundary_errors])
    if previous is not None and (
            previous.get("status") != "complete"
            or opening.get("state_data_status") != "complete"):
        reason_codes.append("prior_state_unresolved")
    deadline = close + dt.timedelta(
        hours=policy["timestamp_policy"]["maximum_recording_delay_hours_for_complete"])
    if generated > deadline:
        reason_codes.append("complete_recording_deadline_elapsed")
    prepared: list[dict] = []
    if not reason_codes and bars is not None:
        if _bar_for(bars, "SPY", session_date) is None:
            reason_codes.append("session_spy_bar_missing")
        if replay_spec["role"] != "benchmark":
            prepared = _prepare_decisions(rows, instruments, replay_spec)
            required_symbols = {
                decision["symbol"] for decision in prepared if decision["eligible_signal"]
            } | {position["symbol"] for position in opening["open_positions"]}
            for symbol in sorted(required_symbols):
                if _bar_for(bars, symbol, session_date) is None:
                    reason_codes.append("required_session_bar_missing:" + symbol)
    if reason_codes:
        terminal = _carry_unavailable(opening, session_date)
        result = _assemble_result(
            replay_spec=replay_spec, session=session_date, close_utc=_format_utc(close),
            generated_at_utc=generated_text, data_cutoff_utc=cutoff_text,
            status="unavailable", reason_codes=reason_codes, previous=previous,
            manifest=manifest, denominator=_unavailable_denominator(), decisions=[], events=[],
            opening=opening, terminal=terminal, entry_cost=0.0, exit_cost=0.0,
            entry_count=0, exit_count=0)
    else:
        if bars is None:
            raise ForwardError("market_bars_unexpectedly_missing")
        if replay_spec["role"] == "benchmark":
            terminal, events, entry_cost, exit_cost, entry_count, exit_count = (
                _benchmark_transition(
                    opening=opening, bars=bars, session=session_date,
                    replay_spec=replay_spec, cost_policy=cost_policy))
            decisions = []
            denominator = _benchmark_denominator()
            complete_reasons = ["complete_continuous_spy"]
        else:
            terminal, decisions, events, entry_cost, exit_cost, entry_count, exit_count = (
                _candidate_transition(
                    opening=opening, rows=rows, instruments=instruments, bars=bars,
                    session=session_date, replay_spec=replay_spec, cost_policy=cost_policy,
                    portfolio_policy=portfolio_policy))
            denominator = _complete_denominator(
                sorted(rows, key=lambda value: value["event_id"]), decisions)
            complete_reasons = (["complete_no_source_events"] if not rows else
                                ["complete_no_eligible_signals"]
                                if denominator["zero_signal_session"] else ["complete"])
        result = _assemble_result(
            replay_spec=replay_spec, session=session_date, close_utc=_format_utc(close),
            generated_at_utc=generated_text, data_cutoff_utc=cutoff_text,
            status="complete", reason_codes=complete_reasons, previous=previous,
            manifest=manifest, denominator=denominator, decisions=decisions, events=events,
            opening=opening, terminal=terminal, entry_cost=entry_cost, exit_cost=exit_cost,
            entry_count=entry_count, exit_count=exit_count)
    if validate_output:
        errors = _validate_forward_result_structure(result, replay_spec, previous)
        if errors:
            raise ForwardError("forward_result_self_validation:" + errors[0])
    return result


def build_forward_result(*, replay_spec: dict, session_date: str,
                         feature_input_path: Path, bars_path: Path,
                         previous_result_path: Path | None = None,
                         generated_at_utc: str | None = None,
                         data_cutoff_utc: str | None = None,
                         input_root: Path = ROOT, policy_path: Path = POLICY_DEFAULT,
                         cost_policy_path: Path = COST_POLICY_DEFAULT,
                         portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                         calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                         repository_root: Path = ROOT) -> dict:
    return _build_forward_result_impl(
        replay_spec=replay_spec, session_date=session_date,
        feature_input_path=feature_input_path, bars_path=bars_path,
        previous_result_path=previous_result_path, generated_at_utc=generated_at_utc,
        data_cutoff_utc=data_cutoff_utc, input_root=input_root, policy_path=policy_path,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path, repository_root=repository_root,
        validate_output=True)


def _validate_position(position: Any, index: int,
                       stop_slippage_bps: float | None = None) -> list[str]:
    prefix = f"position[{index}]"
    if not isinstance(position, dict):
        return [prefix + ":not_object"]
    errors: list[str] = []
    if set(position) != _POSITION_KEYS:
        errors.append(prefix + ":keys_invalid")
    if (not isinstance(position.get("position_id"), str)
            or not _ID_RE.fullmatch(position.get("position_id", ""))):
        errors.append(prefix + ":id_invalid")
    if position.get("position_kind") not in {"catalyst_signal", "continuous_spy"}:
        errors.append(prefix + ":kind_invalid")
    if not isinstance(position.get("symbol"), str) or not _SYMBOL_RE.fullmatch(
            position.get("symbol", "")):
        errors.append(prefix + ":symbol_invalid")
    if position.get("instrument_type") not in SUPPORTED_INSTRUMENTS:
        errors.append(prefix + ":instrument_invalid")
    if _parse_date(position.get("entry_session_date")) is None:
        errors.append(prefix + ":entry_date_invalid")
    for key in ("entry_price", "entry_notional", "units", "last_mark_price", "market_value"):
        if not _number(position.get(key), positive=True):
            errors.append(f"{prefix}:{key}_invalid")
    for key in ("entry_cost", "cost_rate_per_side", "planned_stop_risk"):
        if not _number(position.get(key), nonnegative=True):
            errors.append(f"{prefix}:{key}_invalid")
    if (not isinstance(position.get("held_sessions"), int)
            or isinstance(position.get("held_sessions"), bool)
            or position.get("held_sessions", -1) < 1):
        errors.append(prefix + ":held_sessions_invalid")
    if position.get("position_kind") == "catalyst_signal":
        for key in ("max_entry", "stop_price", "target_price"):
            if not _number(position.get(key), positive=True):
                errors.append(f"{prefix}:{key}_invalid")
        if (not isinstance(position.get("max_hold_sessions"), int)
                or isinstance(position.get("max_hold_sessions"), bool)
                or not 1 <= position.get("max_hold_sessions", 0) <= 10
                or (isinstance(position.get("held_sessions"), int)
                    and position["held_sessions"] >= position.get("max_hold_sessions", 0))):
            errors.append(prefix + ":hold_contract_invalid")
        if all(_number(position.get(key), positive=True)
               for key in ("entry_price", "stop_price", "target_price")) and not (
                float(position["stop_price"]) < float(position["entry_price"])
                < float(position["target_price"])):
            errors.append(prefix + ":levels_invalid")
        if (stop_slippage_bps is not None
                and _number(stop_slippage_bps, nonnegative=True)
                and all(_number(position.get(key), positive=True)
                        for key in ("entry_price", "entry_notional", "stop_price"))
                and _number(position.get("cost_rate_per_side"), nonnegative=True)
                and _number(position.get("planned_stop_risk"), nonnegative=True)):
            modeled_stop_fill = float(position["stop_price"]) * (
                1.0 - float(stop_slippage_bps) / 10000.0)
            expected_risk = float(position["entry_notional"]) * (
                (float(position["entry_price"]) - modeled_stop_fill)
                / float(position["entry_price"])
                + 2.0 * float(position["cost_rate_per_side"]))
            if abs(float(position["planned_stop_risk"]) - expected_risk) > 5e-8:
                errors.append(prefix + ":planned_stop_risk_recurrence_invalid")
    else:
        if (position.get("symbol") != "SPY"
                or any(position.get(key) is not None
                       for key in ("max_entry", "stop_price", "target_price",
                                   "max_hold_sessions"))
                or position.get("planned_stop_risk") != 0.0):
            errors.append(prefix + ":benchmark_contract_invalid")
    if (_number(position.get("units"), positive=True)
            and _number(position.get("last_mark_price"), positive=True)
            and abs(float(position["market_value"])
                    - float(position["units"]) * float(position["last_mark_price"])) > 2e-8):
        errors.append(prefix + ":market_value_recurrence_invalid")
    if position.get("position_sha256") != sha256_obj(_position_payload(position)):
        errors.append(prefix + ":hash_mismatch")
    return errors


def _validate_state(state: Any, name: str, replay_spec: dict | None = None) -> list[str]:
    if not isinstance(state, dict):
        return [name + ":not_object"]
    errors: list[str] = []
    if set(state) != _STATE_KEYS:
        errors.append(name + ":keys_invalid")
    if state.get("state_data_status") not in {
            "genesis", "complete", "unavailable_carried_without_mark"}:
        errors.append(name + ":data_status_invalid")
    if state.get("as_of_session_date") is not None and _parse_date(
            state.get("as_of_session_date")) is None:
        errors.append(name + ":date_invalid")
    if not _number(state.get("normalized_nav"), positive=True):
        errors.append(name + ":normalized_nav_invalid")
    for key in ("cash", "gross_market_value", "gross_exposure", "net_exposure",
                "aggregate_stop_risk", "cumulative_modeled_cost"):
        if not _number(state.get(key), nonnegative=True):
            errors.append(f"{name}:{key}_invalid")
    if (_number(state.get("gross_exposure"), nonnegative=True)
            and float(state["gross_exposure"]) > 1.0 + 2e-8):
        errors.append(name + ":leverage_forbidden")
    positions = state.get("open_positions")
    if not isinstance(positions, list):
        errors.append(name + ":positions_invalid")
        positions = []
    assumptions = (replay_spec.get("simulation_assumptions")
                   if isinstance(replay_spec, dict) else None)
    stop_slippage = (assumptions.get("stop_slippage_bps")
                     if isinstance(assumptions, dict) else None)
    for index, position in enumerate(positions):
        errors.extend(_validate_position(position, index, stop_slippage))
    ids = [value.get("position_id") for value in positions if isinstance(value, dict)]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        errors.append(name + ":position_denominator_invalid")
    if state.get("open_position_count") != len(positions):
        errors.append(name + ":position_count_mismatch")
    position_values_valid = all(
        isinstance(value, dict) and _number(value.get("market_value"), nonnegative=True)
        for value in positions)
    recurrence_inputs_valid = (
        _number(state.get("cash"), nonnegative=True)
        and _number(state.get("gross_market_value"), nonnegative=True)
        and _number(state.get("normalized_nav"), positive=True)
        and _number(state.get("gross_exposure"), nonnegative=True)
        and position_values_valid)
    if recurrence_inputs_valid:
        gross = sum(float(value["market_value"]) for value in positions)
        nav = float(state["cash"]) + gross
        if abs(float(state["gross_market_value"]) - gross) > 2e-8:
            errors.append(name + ":gross_value_mismatch")
        if abs(float(state["normalized_nav"]) - nav) > 2e-8:
            errors.append(name + ":nav_recurrence_invalid")
        if nav > 0:
            exposure = gross / nav
            if (abs(float(state["gross_exposure"]) - exposure) > 2e-8
                    or state.get("net_exposure") != state.get("gross_exposure")):
                errors.append(name + ":exposure_recurrence_invalid")
            risk = sum(float(value["planned_stop_risk"]) for value in positions)
            if (not _number(state.get("aggregate_stop_risk"), nonnegative=True)
                    or abs(float(state["aggregate_stop_risk"]) - risk / nav) > 2e-8):
                errors.append(name + ":stop_risk_recurrence_invalid")
    if state.get("state_sha256") != sha256_obj(_payload(state, "state_sha256")):
        errors.append(name + ":hash_mismatch")
    return errors


def _validate_manifest(manifest: Any, replay_spec: dict) -> list[str]:
    if not isinstance(manifest, dict):
        return ["input_manifest_not_object"]
    errors: list[str] = []
    if set(manifest) != _MANIFEST_KEYS:
        errors.append("input_manifest_keys_invalid")
    if manifest.get("source_kind") not in {"package", "corpus", "unsupported", "unavailable"}:
        errors.append("input_manifest_source_kind_invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or manifest.get("entry_count") != len(entries):
        errors.append("input_manifest_entries_invalid")
        entries = []
    roles: list[str] = []
    for index, ref in enumerate(entries):
        if not isinstance(ref, dict) or set(ref) != _INPUT_REF_KEYS:
            errors.append(f"input_manifest_ref_shape_invalid:{index}")
            continue
        roles.append(ref.get("role"))
        if ref.get("status") not in {"available", "missing", "invalid"}:
            errors.append(f"input_manifest_ref_status_invalid:{index}")
        if not isinstance(ref.get("path"), str) or not ref["path"] or Path(ref["path"]).is_absolute() \
                or ".." in Path(ref["path"]).parts:
            errors.append(f"input_manifest_ref_path_invalid:{index}")
        if ref.get("status") == "available":
            for key in ("file_sha256", "semantic_sha256"):
                if not isinstance(ref.get(key), str) or not _SHA_RE.fullmatch(ref[key]):
                    errors.append(f"input_manifest_ref_hash_invalid:{index}:{key}")
            if not isinstance(ref.get("schema"), str) or ref.get("error") is not None:
                errors.append(f"input_manifest_available_ref_invalid:{index}")
        elif ref.get("error") is None:
            errors.append(f"input_manifest_unavailable_reason_missing:{index}")
    if len(roles) != len(set(roles)):
        errors.append("input_manifest_duplicate_roles")
    if "feature_source" not in roles or "market_bars" not in roles:
        errors.append("input_manifest_required_roles_missing")
    previous_present = "previous_result" in roles
    if manifest.get("previous_result_included") is not previous_present:
        errors.append("input_manifest_previous_flag_mismatch")
    if (manifest.get("replay_spec_sha256") != replay_spec.get("replay_spec_sha256")
            or manifest.get("policy_contracts_sha256") != sha256_obj(
                replay_spec.get("policy_contracts"))):
        errors.append("input_manifest_spec_binding_invalid")
    if manifest.get("manifest_sha256") != sha256_obj(
            _payload(manifest, "manifest_sha256")):
        errors.append("input_manifest_hash_mismatch")
    return errors


def _validate_forward_result_structure(document: Any, replay_spec: dict,
                                       previous_result: dict | None) -> list[str]:
    if not isinstance(document, dict):
        return ["forward_result_not_object"]
    errors: list[str] = []
    if set(document) != _RESULT_KEYS:
        errors.append("forward_result_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"forward_result_safety_invalid:{key}")
    if (document.get("schema") != FORWARD_RESULT_SCHEMA or document.get("tool") != TOOL
            or document.get("algorithm_version") != ALGORITHM_VERSION
            or document.get("artifact_valid") is not True):
        errors.append("forward_result_identity_invalid")
    for key in ("plan_id", "plan_sha256", "strategy_id", "role", "replay_spec_sha256"):
        expected = replay_spec.get(key)
        if document.get(key) != expected:
            errors.append(f"forward_result_spec_binding_invalid:{key}")
    session = _parse_date(document.get("session_date"))
    close = _parse_canonical_utc(document.get("session_close_utc"))
    generated = _parse_canonical_utc(document.get("generated_at_utc"))
    cutoff = _parse_canonical_utc(document.get("data_cutoff_utc"))
    if session is None or close is None or generated is None or cutoff is None \
            or generated < cutoff or cutoff < close:
        errors.append("forward_result_timestamps_invalid")
    if document.get("status") not in {"complete", "unavailable"}:
        errors.append("forward_result_status_invalid")
    reasons = document.get("reason_codes")
    if (not isinstance(reasons, list) or not reasons or reasons != sorted(set(reasons))
            or any(not isinstance(value, str) or not value for value in reasons)):
        errors.append("forward_result_reason_codes_invalid")
    errors.extend(_validate_manifest(document.get("input_manifest"), replay_spec))
    manifest = document.get("input_manifest")
    if (document.get("status") == "complete" and replay_spec.get("role") != "benchmark"
            and isinstance(manifest, dict)
            and manifest.get("source_kind") != "package"):
        errors.append("forward_result_complete_requires_immutable_session_package")
    denominator = document.get("signal_denominator")
    if not isinstance(denominator, dict) or set(denominator) != _DENOMINATOR_KEYS:
        errors.append("forward_result_denominator_invalid")
        denominator = {}
    else:
        if denominator.get("source_status") not in {"available", "unavailable", "not_applicable"}:
            errors.append("forward_result_denominator_status_invalid")
        for key in ("source_row_count", "eligible_row_count", "ineligible_row_count",
                    "generated_signal_count", "entered_signal_count", "excluded_signal_count",
                    "decision_count"):
            if not isinstance(denominator.get(key), int) or isinstance(
                    denominator.get(key), bool) or denominator[key] < 0:
                errors.append(f"forward_result_denominator_count_invalid:{key}")
        if (denominator.get("source_row_count") != denominator.get("eligible_row_count", 0)
                + denominator.get("ineligible_row_count", 0)
                or denominator.get("source_row_count") != denominator.get(
                    "excluded_signal_count", 0) + denominator.get("entered_signal_count", 0)):
            errors.append("forward_result_denominator_recurrence_invalid")
        event_ids = denominator.get("source_event_ids")
        if (not isinstance(event_ids, list) or event_ids != sorted(event_ids)
                or len(event_ids) != len(set(event_ids))
                or len(event_ids) != denominator.get("source_row_count")):
            errors.append("forward_result_event_denominator_invalid")
        if denominator.get("denominator_sha256") != sha256_obj(
                _payload(denominator, "denominator_sha256")):
            errors.append("forward_result_denominator_hash_mismatch")
    decisions = document.get("decisions")
    if not isinstance(decisions, list):
        errors.append("forward_result_decisions_invalid")
        decisions = []
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
            errors.append(f"forward_decision_shape_invalid:{index}")
            continue
        if (not isinstance(decision.get("levels"), dict)
                or set(decision["levels"]) != _LEVEL_KEYS
                or not isinstance(decision.get("cost"), dict)
                or set(decision["cost"]) != _DECISION_COST_KEYS):
            errors.append(f"forward_decision_nested_shape_invalid:{index}")
        if decision.get("status") not in {"entered", "not_entered", "ineligible"}:
            errors.append(f"forward_decision_status_invalid:{index}")
        if type(decision.get("eligible_signal")) is not bool:
            errors.append(f"forward_decision_eligibility_invalid:{index}")
        if (not isinstance(decision.get("decision_id"), str)
                or not _ID_RE.fullmatch(decision.get("decision_id", ""))
                or not isinstance(decision.get("event_id"), str)
                or not _ID_RE.fullmatch(decision.get("event_id", ""))):
            errors.append(f"forward_decision_id_invalid:{index}")
        if (not isinstance(decision.get("symbol"), str)
                or not _SYMBOL_RE.fullmatch(decision.get("symbol", ""))):
            errors.append(f"forward_decision_symbol_invalid:{index}")
        if decision.get("instrument_type") not in SUPPORTED_INSTRUMENTS:
            errors.append(f"forward_decision_instrument_invalid:{index}")
        if (not isinstance(decision.get("priority"), int)
                or isinstance(decision.get("priority"), bool)
                or decision.get("priority", 0) < 1):
            errors.append(f"forward_decision_priority_invalid:{index}")
        decision_reasons = decision.get("reason_codes")
        if (not isinstance(decision_reasons, list) or not decision_reasons
                or decision_reasons != sorted(set(decision_reasons))
                or any(not isinstance(value, str) or not value
                       for value in decision_reasons)):
            errors.append(f"forward_decision_reason_codes_invalid:{index}")
        if decision.get("status") == "entered":
            if (decision.get("eligible_signal") is not True
                    or not _number(decision.get("allocated_weight"), positive=True)
                    or not isinstance(decision.get("position_id"), str)):
                errors.append(f"forward_decision_entered_contract_invalid:{index}")
        elif (decision.get("allocated_weight") is not None
              or decision.get("position_id") is not None):
            errors.append(f"forward_decision_nonentry_allocation_invalid:{index}")
        if (decision.get("status") == "ineligible"
                and decision.get("eligible_signal") is not False):
            errors.append(f"forward_decision_ineligible_contract_invalid:{index}")
        if decision.get("decision_sha256") != sha256_obj(
                _payload(decision, "decision_sha256")):
            errors.append(f"forward_decision_hash_mismatch:{index}")
    if denominator and (denominator.get("decision_count") != len(decisions)
                        or denominator.get("source_event_ids") != [
                            value.get("event_id") if isinstance(value, dict) else None
                            for value in decisions]):
        errors.append("forward_result_decision_denominator_mismatch")
    events = document.get("position_events")
    if not isinstance(events, list):
        errors.append("forward_result_position_events_invalid")
        events = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != _POSITION_EVENT_KEYS:
            errors.append(f"position_event_shape_invalid:{index}")
            continue
        if event.get("action") not in {"held", "exited"}:
            errors.append(f"position_event_action_invalid:{index}")
        if type(event.get("was_entered_this_session")) is not bool:
            errors.append(f"position_event_entry_flag_invalid:{index}")
        for key in ("gross_value", "exit_cost"):
            if not _number(event.get(key), nonnegative=True):
                errors.append(f"position_event_value_invalid:{index}:{key}")
        if (not isinstance(event.get("held_sessions"), int)
                or isinstance(event.get("held_sessions"), bool)
                or event.get("held_sessions", 0) < 1):
            errors.append(f"position_event_held_sessions_invalid:{index}")
        if event.get("action") == "exited":
            if (not isinstance(event.get("exit_reason"), str)
                    or not _number(event.get("exit_price"), positive=True)
                    or event.get("mark_price") is not None):
                errors.append(f"position_event_exit_contract_invalid:{index}")
        elif (event.get("exit_reason") is not None or event.get("exit_price") is not None
              or not _number(event.get("mark_price"), positive=True)):
            errors.append(f"position_event_hold_contract_invalid:{index}")
        if event.get("event_sha256") != sha256_obj(_payload(event, "event_sha256")):
            errors.append(f"position_event_hash_mismatch:{index}")
    opening = document.get("opening_state")
    terminal = document.get("terminal_state")
    errors.extend(_validate_state(opening, "opening_state", replay_spec))
    errors.extend(_validate_state(terminal, "terminal_state", replay_spec))
    if isinstance(opening, dict) and isinstance(terminal, dict):
        if previous_result is not None:
            if (document.get("previous_result_sha256") != previous_result.get("result_sha256")
                    or document.get("previous_terminal_state_sha256") != previous_result.get(
                        "terminal_state", {}).get("state_sha256")
                    or opening != previous_result.get("terminal_state")):
                errors.append("forward_result_previous_state_continuity_invalid")
        else:
            genesis = (document.get("session_date") == replay_spec.get("start_session_date"))
            for key in ("previous_result_sha256", "previous_terminal_state_sha256"):
                value = document.get(key)
                if genesis and value is not None:
                    errors.append(f"forward_result_genesis_previous_hash_invalid:{key}")
                if (not genesis and (not isinstance(value, str)
                                     or not _SHA_RE.fullmatch(value))):
                    errors.append(f"forward_result_non_genesis_previous_hash_invalid:{key}")
        if document.get("status") == "complete" and terminal.get("state_data_status") != "complete":
            errors.append("forward_result_complete_state_status_invalid")
        if (document.get("status") == "unavailable"
                and terminal.get("state_data_status") != "unavailable_carried_without_mark"):
            errors.append("forward_result_unavailable_state_status_invalid")
    nav_rows = document.get("nav_rows")
    if not isinstance(nav_rows, list) or len(nav_rows) != 2:
        errors.append("forward_result_nav_rows_invalid")
        nav_rows = []
    for index, row in enumerate(nav_rows):
        if not isinstance(row, dict) or set(row) != _NAV_ROW_KEYS:
            errors.append(f"forward_nav_row_shape_invalid:{index}")
            continue
        for key in ("normalized_nav", "cash", "gross_market_value", "gross_exposure",
                    "entry_cost", "exit_cost"):
            if not _number(row.get(key), positive=(key == "normalized_nav"),
                           nonnegative=(key != "normalized_nav")):
                errors.append(f"forward_nav_row_value_invalid:{index}:{key}")
        if row.get("row_sha256") != sha256_obj(_payload(row, "row_sha256")):
            errors.append(f"forward_nav_row_hash_mismatch:{index}")
    nav_recurrence_inputs_valid = (
        len(nav_rows) == 2 and isinstance(opening, dict) and isinstance(terminal, dict)
        and _number(opening.get("normalized_nav"), positive=True)
        and _number(terminal.get("normalized_nav"), positive=True))
    if nav_recurrence_inputs_valid:
        recurrence = (float(terminal["normalized_nav"])
                      / float(opening["normalized_nav"]) - 1.0)
        state_fields = ("normalized_nav", "cash", "gross_market_value",
                        "gross_exposure", "state_sha256")
        if (nav_rows[0].get("point") != "opening"
                or nav_rows[1].get("point") != "terminal"
                or nav_rows[0].get("session_date") != document.get("session_date")
                or nav_rows[1].get("session_date") != document.get("session_date")
                or any(nav_rows[0].get(key) != opening.get(key) for key in state_fields)
                or any(nav_rows[1].get(key) != terminal.get(key) for key in state_fields)
                or nav_rows[0].get("session_return") is not None
                or nav_rows[0].get("return_available") is not False
                or nav_rows[0].get("entry_cost") != 0.0
                or nav_rows[0].get("exit_cost") != 0.0):
            errors.append("forward_result_nav_recurrence_invalid")
        if document.get("status") == "complete":
            if (not _number(document.get("net_return"))
                    or not _number(nav_rows[1].get("session_return"))
                    or abs(float(document["net_return"]) - recurrence) > 2e-8
                    or abs(float(nav_rows[1]["session_return"]) - recurrence) > 2e-8
                    or nav_rows[1].get("return_available") is not True):
                errors.append("forward_result_complete_return_recurrence_invalid")
        elif (document.get("net_return") is not None
              or nav_rows[1].get("session_return") is not None
              or nav_rows[1].get("return_available") is not False):
            errors.append("forward_result_unavailable_return_must_be_null")
    if isinstance(terminal, dict) and _number(
            terminal.get("gross_exposure"), nonnegative=True):
        if (document.get("gross_exposure") != terminal.get("gross_exposure")
                or document.get("active") is not (
                    float(terminal["gross_exposure"]) > 1e-12)):
            errors.append("forward_result_exposure_summary_invalid")
    elif isinstance(terminal, dict):
        errors.append("forward_result_exposure_summary_invalid")
    costs = document.get("modeled_costs")
    if not isinstance(costs, dict) or set(costs) != _MODELED_COST_KEYS:
        errors.append("forward_result_modeled_costs_invalid")
    else:
        for key in ("entry_cost", "exit_cost", "total_session_cost",
                    "cumulative_modeled_cost"):
            if not _number(costs.get(key), nonnegative=True):
                errors.append(f"forward_result_cost_invalid:{key}")
        if all(_number(costs.get(key), nonnegative=True)
               for key in ("total_session_cost", "entry_cost", "exit_cost")):
            if abs(float(costs["total_session_cost"]) - float(
                    costs["entry_cost"]) - float(costs["exit_cost"])) > 2e-8:
                errors.append("forward_result_session_cost_recurrence_invalid")
        if isinstance(terminal, dict) and costs.get("cumulative_modeled_cost") != terminal.get(
                "cumulative_modeled_cost"):
            errors.append("forward_result_cumulative_cost_mismatch")
        if (isinstance(opening, dict) and isinstance(terminal, dict)
                and _number(opening.get("cumulative_modeled_cost"), nonnegative=True)
                and _number(terminal.get("cumulative_modeled_cost"), nonnegative=True)
                and _number(costs.get("total_session_cost"), nonnegative=True)
                and abs(float(terminal["cumulative_modeled_cost"])
                        - float(opening["cumulative_modeled_cost"])
                        - float(costs["total_session_cost"])) > 2e-8):
            errors.append("forward_result_cumulative_cost_recurrence_invalid")
        for key in ("entry_count", "exit_count"):
            if (not isinstance(costs.get(key), int) or isinstance(costs.get(key), bool)
                    or costs.get(key, -1) < 0):
                errors.append("forward_result_cost_count_invalid:" + key)
        if len(nav_rows) == 2 and all(isinstance(row, dict) for row in nav_rows):
            if (nav_rows[1].get("entry_cost") != costs.get("entry_cost")
                    or nav_rows[1].get("exit_cost") != costs.get("exit_cost")):
                errors.append("forward_result_nav_cost_binding_invalid")
        valid_events = [event for event in events if isinstance(event, dict)]
        if (isinstance(costs.get("entry_count"), int)
                and costs.get("entry_count") != sum(
                    event.get("was_entered_this_session") is True
                    for event in valid_events)):
            errors.append("forward_result_entry_count_recurrence_invalid")
        if (isinstance(costs.get("exit_count"), int)
                and costs.get("exit_count") != sum(
                    event.get("action") == "exited" for event in valid_events)):
            errors.append("forward_result_exit_count_recurrence_invalid")
        if (all(_number(event.get("exit_cost"), nonnegative=True)
                for event in valid_events)
                and _number(costs.get("exit_cost"), nonnegative=True)
                and abs(float(costs["exit_cost"]) - sum(
                    float(event["exit_cost"]) for event in valid_events)) > 2e-8):
            errors.append("forward_result_exit_cost_recurrence_invalid")
    if document.get("status") == "unavailable":
        if isinstance(opening, dict) and isinstance(terminal, dict):
            try:
                if terminal != _carry_unavailable(
                        opening, str(document.get("session_date"))):
                    errors.append("forward_result_unavailable_state_carry_mismatch")
            except (ForwardError, KeyError, TypeError, ValueError, OverflowError):
                errors.append("forward_result_unavailable_state_carry_unverifiable")
        if decisions or events:
            errors.append("forward_result_unavailable_has_decisions_or_events")
        if denominator and (denominator.get("source_status") != "unavailable"
                            or denominator.get("zero_signal_session") is not None
                            or any(denominator.get(key) != 0 for key in (
                                "source_row_count", "eligible_row_count",
                                "ineligible_row_count", "generated_signal_count",
                                "entered_signal_count", "excluded_signal_count",
                                "decision_count"))):
            errors.append("forward_result_unavailable_denominator_invalid")
        if isinstance(costs, dict) and any(costs.get(key) != 0 for key in (
                "entry_cost", "exit_cost", "total_session_cost", "entry_count",
                "exit_count")):
            errors.append("forward_result_unavailable_session_cost_invalid")
    elif document.get("status") == "complete" and denominator:
        expected_source = ("not_applicable" if replay_spec.get("role") == "benchmark"
                           else "available")
        if denominator.get("source_status") != expected_source:
            errors.append("forward_result_complete_denominator_status_invalid")
    if document.get("limitations") != LIMITATIONS:
        errors.append("forward_result_limitations_invalid")
    if document.get("result_sha256") != sha256_obj(_payload(document, "result_sha256")):
        errors.append("forward_result_hash_mismatch")
    return sorted(set(errors))


def validate_forward_result(document: Any, *, replay_spec: dict,
                            previous_result: dict | None = None,
                            input_root: Path = ROOT, policy_path: Path = POLICY_DEFAULT,
                            cost_policy_path: Path = COST_POLICY_DEFAULT,
                            portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                            calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                            repository_root: Path = ROOT,
                            revalidate_inputs: bool = True,
                            _chain_paths: frozenset[str] = frozenset(),
                            _chain_depth: int = 0) -> list[str]:
    spec_errors = validate_replay_spec(
        replay_spec, repository_root=repository_root, policy_path=policy_path,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path)
    if spec_errors:
        return ["replay_spec_invalid:" + spec_errors[0]]
    try:
        errors = _validate_forward_result_structure(document, replay_spec, previous_result)
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["forward_result_malformed_nested_value"]
    if isinstance(document, dict):
        session = _parse_date(document.get("session_date"))
        if session is not None:
            try:
                if document.get("session_close_utc") != _format_utc(
                        _session_close(session, calendar_policy_path)):
                    errors.append("forward_result_session_close_mismatch")
            except ForwardError:
                errors.append("forward_result_session_close_unverifiable")
    if errors or not revalidate_inputs:
        return sorted(set(errors))
    try:
        entries = {ref["role"]: ref for ref in document["input_manifest"]["entries"]}
        previous_path = (Path(entries["previous_result"]["path"])
                         if "previous_result" in entries else None)
        expected = _build_forward_result_impl(
            replay_spec=replay_spec, session_date=document["session_date"],
            feature_input_path=Path(entries["feature_source"]["path"]),
            bars_path=Path(entries["market_bars"]["path"]),
            previous_result_path=previous_path,
            generated_at_utc=document["generated_at_utc"],
            data_cutoff_utc=document["data_cutoff_utc"], input_root=input_root,
            policy_path=policy_path, cost_policy_path=cost_policy_path,
            portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path, repository_root=repository_root,
            validate_output=False, chain_paths=_chain_paths, chain_depth=_chain_depth)
        if expected != document:
            errors.append("forward_result_semantic_rederivation_mismatch")
    except (KeyError, ForwardError, OSError, TypeError, ValueError, OverflowError):
        errors.append("forward_result_input_revalidation_failed")
    return sorted(set(errors))


def _result_ref(path: Path, replay_spec: dict, session_date: str, input_root: Path,
                maximum_bytes: int, *, revalidate: bool,
                policy_path: Path = POLICY_DEFAULT,
                cost_policy_path: Path = COST_POLICY_DEFAULT,
                portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                repository_root: Path = ROOT) -> tuple[dict, dict]:
    ref, result = _input_ref(path, "forward_result:" + replay_spec["strategy_id"],
                             input_root, maximum_bytes)
    if result is None or ref["status"] != "available":
        raise ForwardError("run_result_unavailable:" + replay_spec["strategy_id"])
    errors = validate_forward_result(
        result, replay_spec=replay_spec, input_root=input_root,
        policy_path=policy_path, cost_policy_path=cost_policy_path,
        portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path, repository_root=repository_root,
        revalidate_inputs=revalidate)
    if errors:
        raise ForwardError("run_result_invalid:" + replay_spec["strategy_id"] + ":" + errors[0])
    if result.get("session_date") != session_date:
        raise ForwardError("run_result_session_mismatch:" + replay_spec["strategy_id"])
    return {
        "role": replay_spec["role"], "strategy_id": replay_spec["strategy_id"],
        "path": ref["path"], "file_sha256": ref["file_sha256"],
        "result_sha256": result["result_sha256"],
        "replay_spec_sha256": replay_spec["replay_spec_sha256"],
        "status": result["status"],
    }, result


def _run_denominator(specs: list[dict], refs: list[dict]) -> dict:
    commitment = specs[0]["expected_run_denominator"]
    candidate_id = commitment["candidate_strategy_id"]
    benchmark_id = commitment["benchmark_strategy_id"]
    incumbents = list(commitment["incumbent_strategy_ids"])
    expected = list(commitment["expected_strategy_ids"])
    observed = [ref["strategy_id"] for ref in refs]
    document = {
        "candidate_strategy_id": candidate_id,
        "benchmark_strategy_id": benchmark_id,
        "incumbent_strategy_ids": incumbents,
        "expected_strategy_ids": expected,
        "expected_count": commitment["expected_count"], "result_count": len(refs),
        "all_present_once": observed == expected and len(observed) == len(set(observed)),
        "expected_run_denominator_sha256": commitment["commitment_sha256"],
        "denominator_sha256": "",
    }
    document["denominator_sha256"] = sha256_obj(
        _payload(document, "denominator_sha256"))
    return document


def _ordered_run_specs(specs: Any) -> list[dict] | None:
    if not isinstance(specs, list) or not specs or any(
            not isinstance(spec, dict) for spec in specs):
        return None
    candidates = [spec for spec in specs if spec.get("role") == "candidate"]
    benchmarks = [spec for spec in specs if spec.get("role") == "benchmark"]
    incumbents = [spec for spec in specs if spec.get("role") == "incumbent"]
    ids = [spec.get("strategy_id") for spec in specs]
    if (len(candidates) != 1 or len(benchmarks) != 1 or not incumbents
            or any(not isinstance(value, str) for value in ids)
            or len(ids) != len(set(ids))
            or len(candidates) + len(benchmarks) + len(incumbents) != len(specs)):
        return None
    return [candidates[0], benchmarks[0],
            *sorted(incumbents, key=lambda value: value["strategy_id"])]


def _run_spec_comparability_errors(specs: list[dict]) -> list[str]:
    if not specs:
        return ["run_specs_missing"]
    errors: list[str] = []
    for key in ("start_session_date", "simulation_assumptions", "policy_contracts",
                "expected_run_denominator"):
        values = [spec.get(key) for spec in specs]
        if any(value != values[0] for value in values[1:]):
            errors.append("run_spec_comparison_contract_mismatch:" + key)
    commitment = specs[0].get("expected_run_denominator")
    if isinstance(commitment, dict):
        expected_pairs = [
            ("candidate", commitment.get("candidate_strategy_id")),
            ("benchmark", commitment.get("benchmark_strategy_id")),
            *[("incumbent", value)
              for value in commitment.get("incumbent_strategy_ids", [])],
        ]
        observed_pairs = [(spec.get("role"), spec.get("strategy_id")) for spec in specs]
        if observed_pairs != expected_pairs:
            errors.append("run_spec_set_does_not_match_frozen_denominator")
    else:
        errors.append("run_expected_denominator_missing")
    return errors


def _run_status_and_reason_codes(refs: list[dict], *, exact_denominator: bool
                                 ) -> tuple[str, list[str]]:
    if not exact_denominator:
        return "unavailable", ["run_result_denominator_incomplete"]
    unavailable: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            unavailable.add("strategy_unavailable:invalid_ref_" + str(index))
        elif ref.get("status") != "complete":
            unavailable.add("strategy_unavailable:" + str(ref.get("strategy_id")))
    unavailable_reasons = sorted(unavailable)
    if unavailable_reasons:
        return "unavailable", unavailable_reasons
    return "complete", ["complete_all_strategies"]


def _manifest_entry(result: dict, role: str) -> dict | None:
    manifest = result.get("input_manifest")
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    matches = ([entry for entry in entries
                if isinstance(entry, dict) and entry.get("role") == role]
               if isinstance(entries, list) else [])
    return matches[0] if len(matches) == 1 else None


def _run_common_result_errors(results: list[dict], specs: list[dict],
                              session_date: str) -> list[str]:
    if len(results) != len(specs) or any(not isinstance(value, dict) for value in results):
        return ["run_result_common_input_denominator_invalid"]
    errors: list[str] = []
    for key in ("session_date", "session_close_utc", "data_cutoff_utc"):
        values = [result.get(key) for result in results]
        expected = session_date if key == "session_date" else values[0]
        if not values or values[0] != expected or any(value != values[0]
                                                      for value in values[1:]):
            errors.append("run_result_common_session_contract_mismatch:" + key)
    market_refs = [_manifest_entry(result, "market_bars") for result in results]
    if (any(ref is None for ref in market_refs)
            or any(ref != market_refs[0] for ref in market_refs[1:])):
        errors.append("run_market_bars_artifact_mismatch")
    catalyst_pairs = [(result, spec) for result, spec in zip(results, specs)
                      if spec.get("role") != "benchmark"]
    feature_contracts: list[tuple[Any, Any]] = []
    for result, _spec in catalyst_pairs:
        manifest = result.get("input_manifest")
        entries = manifest.get("entries") if isinstance(manifest, dict) else None
        package_refs = (sorted(
            [entry for entry in entries if isinstance(entry, dict)
             and (entry.get("role") == "feature_source"
                  or str(entry.get("role", "")).startswith("feature_package_"))],
            key=lambda value: value.get("role", ""))
                        if isinstance(entries, list) else None)
        feature_contracts.append((
            manifest.get("source_kind") if isinstance(manifest, dict) else None,
            package_refs,
        ))
    if (not feature_contracts or any(value[0] != "package" for value in feature_contracts)
            or any(value != feature_contracts[0] for value in feature_contracts[1:])):
        errors.append("run_catalyst_feature_source_or_cutoff_contract_mismatch")
    return sorted(set(errors))


def build_run_receipt(*, plan_id: str, plan_sha256: str, session_date: str,
                      replay_specs: list[dict], result_paths: dict[str, Path],
                      generated_at_utc: str | None = None, input_root: Path = ROOT,
                      policy_path: Path = POLICY_DEFAULT,
                      cost_policy_path: Path = COST_POLICY_DEFAULT,
                      portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                      calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                      repository_root: Path = ROOT) -> dict:
    if not isinstance(replay_specs, list) or not replay_specs:
        raise ForwardError("run_replay_specs_missing")
    for spec in replay_specs:
        errors = validate_replay_spec(
            spec, repository_root=repository_root, policy_path=policy_path,
            cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path)
        if errors:
            raise ForwardError("run_replay_spec_invalid:" + errors[0])
        if spec["plan_id"] != plan_id or spec["plan_sha256"] != plan_sha256:
            raise ForwardError("run_plan_binding_mismatch")
    ordered_specs = _ordered_run_specs(replay_specs)
    if ordered_specs is None:
        raise ForwardError("run_strategy_denominator_invalid")
    comparison_errors = _run_spec_comparability_errors(ordered_specs)
    if comparison_errors:
        raise ForwardError(comparison_errors[0])
    ids = [spec["strategy_id"] for spec in ordered_specs]
    if set(result_paths) != set(ids):
        raise ForwardError("run_result_path_denominator_mismatch")
    policy, errors = load_policy(policy_path, root=repository_root)
    if policy is None or errors:
        raise ForwardError("forward_policy_invalid")
    refs: list[dict] = []
    results: list[dict] = []
    for spec in ordered_specs:
        ref, result = _result_ref(
            Path(result_paths[spec["strategy_id"]]), spec, session_date, input_root,
            policy["path_policy"]["maximum_input_bytes"], revalidate=True,
            policy_path=policy_path, cost_policy_path=cost_policy_path,
            portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path, repository_root=repository_root)
        refs.append(ref)
        results.append(result)
    common_errors = _run_common_result_errors(results, ordered_specs, session_date)
    if common_errors:
        raise ForwardError(common_errors[0])
    generated_text = _trusted_utc(generated_at_utc)
    generated = _parse_utc(generated_text)
    result_times = [_parse_utc(result["generated_at_utc"]) for result in results]
    if generated is None or any(value is None or generated < value for value in result_times):
        raise ForwardError("run_generated_at_precedes_result")
    denominator = _run_denominator(ordered_specs, refs)
    if not denominator["all_present_once"]:
        raise ForwardError("run_result_denominator_incomplete")
    candidate_ref, benchmark_ref, *incumbent_refs = refs
    status, reasons = _run_status_and_reason_codes(refs, exact_denominator=True)
    commitment_sha256 = ordered_specs[0]["expected_run_denominator"][
        "commitment_sha256"]
    document = {
        **SAFETY,
        "schema": RUN_SCHEMA, "tool": TOOL, "algorithm_version": ALGORITHM_VERSION,
        "artifact_valid": True, "plan_id": plan_id, "plan_sha256": plan_sha256,
        "session_date": session_date, "generated_at_utc": generated_text,
        "status": status, "reason_codes": reasons, "candidate": candidate_ref,
        "benchmark": benchmark_ref, "incumbents": incumbent_refs,
        "expected_run_denominator_sha256": commitment_sha256,
        "result_denominator": denominator, "results_sha256": sha256_obj(refs),
        "limitations": list(LIMITATIONS), "run_sha256": "",
    }
    document["run_sha256"] = sha256_obj(_payload(document, "run_sha256"))
    validation = validate_run_receipt(
        document, replay_specs=replay_specs, input_root=input_root, policy_path=policy_path,
        cost_policy_path=cost_policy_path, portfolio_policy_path=portfolio_policy_path,
        calendar_policy_path=calendar_policy_path, repository_root=repository_root)
    if validation:
        raise ForwardError("run_receipt_self_validation:" + validation[0])
    return document


def _validate_run_receipt_impl(document: Any, *, replay_specs: list[dict],
                               input_root: Path, policy_path: Path,
                               cost_policy_path: Path, portfolio_policy_path: Path,
                               calendar_policy_path: Path, repository_root: Path,
                               revalidate_results: bool) -> list[str]:
    if not isinstance(document, dict):
        return ["forward_run_not_object"]
    errors: list[str] = []
    if set(document) != _RUN_KEYS:
        errors.append("forward_run_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"forward_run_safety_invalid:{key}")
    if (document.get("schema") != RUN_SCHEMA or document.get("tool") != TOOL
            or document.get("algorithm_version") != ALGORITHM_VERSION
            or document.get("artifact_valid") is not True):
        errors.append("forward_run_identity_invalid")
    if not isinstance(replay_specs, list):
        return sorted(set(errors + ["forward_run_replay_specs_invalid"]))
    for index, spec in enumerate(replay_specs):
        spec_errors = validate_replay_spec(
            spec, repository_root=repository_root, policy_path=policy_path,
            cost_policy_path=cost_policy_path,
            portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path)
        if spec_errors:
            errors.append(f"forward_run_replay_spec_invalid:{index}:{spec_errors[0]}")
    ordered_specs = _ordered_run_specs(replay_specs)
    if ordered_specs is None:
        errors.append("forward_run_replay_spec_denominator_invalid")
        ordered_specs = []
    else:
        errors.extend(_run_spec_comparability_errors(ordered_specs))
    expected_plan = {(spec.get("plan_id"), spec.get("plan_sha256"))
                     for spec in replay_specs if isinstance(spec, dict)}
    if expected_plan != {(document.get("plan_id"), document.get("plan_sha256"))}:
        errors.append("forward_run_plan_binding_invalid")
    commitment = (ordered_specs[0].get("expected_run_denominator")
                  if ordered_specs else None)
    commitment_sha256 = (commitment.get("commitment_sha256")
                         if isinstance(commitment, dict) else None)
    if document.get("expected_run_denominator_sha256") != commitment_sha256:
        errors.append("forward_run_expected_denominator_binding_invalid")
    session = _parse_date(document.get("session_date"))
    generated = _parse_canonical_utc(document.get("generated_at_utc"))
    if session is None or generated is None:
        errors.append("forward_run_timestamp_invalid")
    incumbents = document.get("incumbents")
    if not isinstance(incumbents, list):
        errors.append("forward_run_incumbents_invalid")
        incumbents = []
    refs = [document.get("candidate"), document.get("benchmark"), *incumbents]
    expected_ids = [spec["strategy_id"] for spec in ordered_specs]
    observed_ids = [ref.get("strategy_id") if isinstance(ref, dict) else None for ref in refs]
    if (not ordered_specs or len(refs) != len(ordered_specs)
            or observed_ids != expected_ids or len(observed_ids) != len(set(observed_ids))):
        errors.append("forward_run_result_denominator_exactness_invalid")
    loaded_policy, loaded_errors = load_policy(policy_path, root=repository_root)
    if loaded_policy is None or loaded_errors:
        errors.append("forward_policy_invalid")
    loaded_results: list[dict | None] = [None] * len(refs)
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict) or set(ref) != _RESULT_REF_KEYS:
            errors.append(f"forward_run_result_ref_invalid:{index}")
            continue
        spec = ordered_specs[index] if index < len(ordered_specs) else None
        if (spec is None or ref.get("strategy_id") != spec.get("strategy_id")
                or ref.get("role") != spec.get("role")
                or ref.get("replay_spec_sha256") != spec.get("replay_spec_sha256")):
            errors.append(f"forward_run_result_ref_binding_invalid:{index}")
        for key in ("file_sha256", "result_sha256", "replay_spec_sha256"):
            if not isinstance(ref.get(key), str) or not _SHA_RE.fullmatch(ref[key]):
                errors.append(f"forward_run_result_ref_hash_invalid:{index}:{key}")
        if ref.get("status") not in {"complete", "unavailable"}:
            errors.append(f"forward_run_result_ref_status_invalid:{index}")
        if spec is not None and loaded_policy is not None and not loaded_errors:
            try:
                expected_ref, result = _result_ref(
                    Path(ref["path"]), spec, document["session_date"], input_root,
                    loaded_policy["path_policy"]["maximum_input_bytes"],
                    revalidate=revalidate_results, policy_path=policy_path,
                    cost_policy_path=cost_policy_path,
                    portfolio_policy_path=portfolio_policy_path,
                    calendar_policy_path=calendar_policy_path,
                    repository_root=repository_root)
                loaded_results[index] = result
                if expected_ref != ref:
                    errors.append(f"forward_run_result_ref_rederivation_mismatch:{index}")
                result_time = _parse_canonical_utc(result["generated_at_utc"])
                if generated is None or result_time is None or generated < result_time:
                    errors.append(f"forward_run_precedes_result:{index}")
            except (ForwardError, KeyError, TypeError, ValueError, OverflowError):
                errors.append(f"forward_run_result_revalidation_failed:{index}")
    if ordered_specs and all(result is not None for result in loaded_results):
        errors.extend(_run_common_result_errors(
            [result for result in loaded_results if result is not None],
            ordered_specs, document.get("session_date")))
    denominator = document.get("result_denominator")
    if not isinstance(denominator, dict) or set(denominator) != _RUN_DENOMINATOR_KEYS:
        errors.append("forward_run_denominator_invalid")
    else:
        if ordered_specs:
            expected_denominator = _run_denominator(
                ordered_specs, [ref for ref in refs if isinstance(ref, dict)])
            if denominator != expected_denominator:
                errors.append("forward_run_denominator_rederivation_mismatch")
            if expected_denominator.get("all_present_once") is not True:
                errors.append("forward_run_denominator_not_complete")
    if document.get("results_sha256") != sha256_obj(refs):
        errors.append("forward_run_results_hash_mismatch")
    commitment_expected_ids = (commitment.get("expected_strategy_ids")
                               if isinstance(commitment, dict) else None)
    exact_denominator = (bool(ordered_specs)
                         and expected_ids == commitment_expected_ids
                         and observed_ids == expected_ids
                         and len(refs) == len(ordered_specs))
    expected_status, expected_reason_codes = _run_status_and_reason_codes(
        refs, exact_denominator=exact_denominator)
    if document.get("status") != expected_status:
        errors.append("forward_run_status_invalid")
    if document.get("reason_codes") != expected_reason_codes:
        errors.append("forward_run_reason_codes_rederivation_mismatch")
    if document.get("limitations") != LIMITATIONS:
        errors.append("forward_run_limitations_invalid")
    if document.get("run_sha256") != sha256_obj(_payload(document, "run_sha256")):
        errors.append("forward_run_hash_mismatch")
    return sorted(set(errors))


def validate_run_receipt(document: Any, *, replay_specs: list[dict],
                         input_root: Path = ROOT, policy_path: Path = POLICY_DEFAULT,
                         cost_policy_path: Path = COST_POLICY_DEFAULT,
                         portfolio_policy_path: Path = PORTFOLIO_POLICY_DEFAULT,
                         calendar_policy_path: Path = CALENDAR_POLICY_DEFAULT,
                         repository_root: Path = ROOT,
                         revalidate_results: bool = True) -> list[str]:
    """Fail closed on every malformed untrusted run artifact."""
    try:
        return _validate_run_receipt_impl(
            document, replay_specs=replay_specs, input_root=input_root,
            policy_path=policy_path, cost_policy_path=cost_policy_path,
            portfolio_policy_path=portfolio_policy_path,
            calendar_policy_path=calendar_policy_path,
            repository_root=repository_root, revalidate_results=revalidate_results)
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["forward_run_malformed_nested_value"]
