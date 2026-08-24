#!/usr/bin/env python3
"""Deterministic comparison-cohort builder and uniqueness-audit handoff.

A cohort locks the strategy registry at the candidate's registration event, includes exactly one
benchmark and every incumbent reference that existed at that sequence, verifies immutable source
bindings and comparable cost contracts, then passes the frozen set to strategy_uniqueness.

No model, network, broker, account, order, production, activation, or profitability authority is
available. Blocked cohorts are valid research terminals and are never audited as if comparable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import strategy_registry as sr  # noqa: E402
from quant import strategy_uniqueness as su  # noqa: E402

TOOL = "quant.strategy_cohort"
COHORT_SCHEMA = "quant_strategy_comparison_cohort/1.0"
AUDIT_SCHEMA = "quant_strategy_cohort_audit/1.0"

SAFETY = {
    **sr.OUTPUT_SAFETY,
    "input_mutation_allowed": False,
    "performance_metrics_emitted_by_cohort": False,
}

_COHORT_KEYS = set(SAFETY) | {
    "tool", "ok", "schema", "cohort_id", "candidate_strategy_id",
    "generated_at_utc", "status", "registry_lock", "policy", "selection_decisions",
    "selected_sources", "comparison_preflight", "blockers", "actual_orders_count",
    "cohort_sha256",
}
_SOURCE_REF_KEYS = {
    "strategy_id", "role", "registration_sequence", "registration_event_sha256",
    "registration_time_utc", "hypothesis_family", "feature_family", "source_path",
    "source_result_sha256", "source_content_sha256", "evaluation_start_date",
    "evaluation_end_date", "cost_stress_multiplier", "cost_policy_version",
    "portfolio_policy_version", "cost_policy_sha256", "portfolio_policy_sha256",
    "evidence_reference",
}
_DECISION_KEYS = {
    "strategy_id", "role_at_lock", "registration_sequence", "included",
    "selected_as", "reason",
}
_AUDIT_KEYS = set(SAFETY) | {
    "tool", "ok", "schema", "cohort_id", "cohort_sha256",
    "candidate_strategy_id", "registry_lock_head_sha256", "classification",
    "uniqueness_result", "uniqueness_result_sha256", "actual_orders_count",
    "audit_sha256",
}


def _payload(document: dict, hash_key: str) -> dict:
    return {key: value for key, value in document.items() if key != hash_key}


def _refuse(error: str, **extra: Any) -> dict:
    return {**SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def _atomic_write_once(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("output_already_exists")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _source_ref(state: dict) -> dict:
    payload = state["registration_payload"]
    return {
        "strategy_id": state["strategy_id"],
        "role": state["role"],
        "registration_sequence": state["registration_sequence"],
        "registration_event_sha256": state["registration_event_sha256"],
        "registration_time_utc": state["registration_time_utc"],
        "hypothesis_family": payload["hypothesis_family"],
        "feature_family": payload["feature_family"],
        "source_path": payload["source_path"],
        "source_result_sha256": payload["source_result_sha256"],
        "source_content_sha256": payload["source_content_sha256"],
        "evaluation_start_date": payload["evaluation_start_date"],
        "evaluation_end_date": payload["evaluation_end_date"],
        "cost_stress_multiplier": payload["cost_stress_multiplier"],
        "cost_policy_version": payload["cost_policy_version"],
        "portfolio_policy_version": payload["portfolio_policy_version"],
        "cost_policy_sha256": payload["cost_policy_sha256"],
        "portfolio_policy_sha256": payload["portfolio_policy_sha256"],
        "evidence_reference": payload["evidence_reference"],
    }


def _selection_decision(state: dict, candidate_id: str) -> dict:
    role = state["role"]
    if state["strategy_id"] == candidate_id:
        included, selected_as, reason = True, "candidate", "candidate_registration_lock"
    elif role == "benchmark_reference":
        included, selected_as, reason = True, "benchmark", "active_benchmark_at_lock"
    elif role == "incumbent_reference":
        included, selected_as, reason = True, "incumbent", "all_incumbents_included_at_lock"
    elif role == "retired_reference":
        included, selected_as, reason = False, None, "retired_before_candidate_lock"
    else:
        included, selected_as, reason = False, None, "other_candidate_not_incumbent_at_lock"
    return {
        "strategy_id": state["strategy_id"],
        "role_at_lock": role,
        "registration_sequence": state["registration_sequence"],
        "included": included,
        "selected_as": selected_as,
        "reason": reason,
    }


def _binding_errors(reference: dict, document: dict) -> list[str]:
    daily = document["daily_path"]
    expected = {
        "source_result_sha256": document["result_sha256"],
        "source_content_sha256": su.sha256_obj(document),
        "evaluation_start_date": daily[0]["date"],
        "evaluation_end_date": daily[-1]["date"],
        "cost_stress_multiplier": float(document["cost_stress_multiplier"]),
        "cost_policy_version": document["policy_versions"]["cost"],
        "portfolio_policy_version": document["policy_versions"]["portfolio"],
        "cost_policy_sha256": document["lineage"]["cost_policy_sha256"],
        "portfolio_policy_sha256": document["lineage"]["portfolio_policy_sha256"],
    }
    return [key for key, value in expected.items() if reference.get(key) != value]


def _contract_key(reference: dict) -> tuple:
    return (
        reference["cost_stress_multiplier"],
        reference["cost_policy_version"],
        reference["portfolio_policy_version"],
        reference["cost_policy_sha256"],
        reference["portfolio_policy_sha256"],
    )


def _load_selected_sources(selected: dict, repository_root: Path,
                           registry_policy: dict) -> tuple[dict[str, dict], list[str]]:
    references = [selected["candidate"], *selected["benchmarks"], *selected["incumbents"]]
    documents: dict[str, dict] = {}
    blockers: list[str] = []
    for reference in references:
        strategy_id = reference["strategy_id"]
        try:
            path = sr.resolve_source(reference["source_path"], repository_root,
                                     registry_policy)
            document = su._read(path)
        except Exception as exc:
            blockers.append(f"source_unreadable:{strategy_id}:{type(exc).__name__}")
            continue
        errors = su._portfolio_errors(document, f"cohort_source:{strategy_id}")
        blockers.extend(f"source_invalid:{strategy_id}:{value}" for value in errors)
        if errors:
            continue
        binding_errors = _binding_errors(reference, document)
        blockers.extend(f"source_binding_mismatch:{strategy_id}:{value}"
                        for value in binding_errors)
        if not binding_errors:
            documents[strategy_id] = document
    return documents, sorted(set(blockers))


def _preflight(selected: dict, documents: dict[str, dict], uniqueness_policy: dict,
               initial_blockers: list[str]) -> tuple[dict, list[str]]:
    references = [selected["candidate"], *selected["benchmarks"], *selected["incumbents"]]
    strategy_ids = [reference["strategy_id"] for reference in references]
    blockers = list(initial_blockers)
    contract_compatible = bool(references) and all(
        _contract_key(reference) == _contract_key(references[0])
        for reference in references[1:])
    if not contract_compatible:
        blockers.append("source_contract_mismatch")

    common_dates: set[str] = set()
    coverage_fractions: dict[str, float] = {}
    candidate_active_count = 0
    if len(documents) == len(strategy_ids):
        date_sets = {
            strategy_id: {row["date"] for row in documents[strategy_id]["daily_path"]}
            for strategy_id in strategy_ids
        }
        common_dates = set.intersection(*date_sets.values()) if date_sets else set()
        if not common_dates:
            blockers.append("common_date_intersection_empty")
        else:
            coverage_fractions = {
                strategy_id: su._round(len(common_dates) / len(date_sets[strategy_id]))
                for strategy_id in sorted(strategy_ids)
            }
            if min(coverage_fractions.values()) < uniqueness_policy[
                    "minimum_common_date_coverage_fraction"]:
                blockers.append("common_date_coverage_below_uniqueness_policy")
            candidate_id = selected["candidate"]["strategy_id"]
            candidate_active_count = sum(
                row["date"] in common_dates and float(row["gross_exposure"]) > 1e-12
                for row in documents[candidate_id]["daily_path"])
    minimum_fraction = min(coverage_fractions.values()) if coverage_fractions else None
    preflight = {
        "selected_source_count": len(strategy_ids),
        "validated_source_count": len(documents),
        "all_source_bindings_valid": len(documents) == len(strategy_ids),
        "source_contract_compatible": contract_compatible,
        "common_date_observation_count": len(common_dates),
        "common_date_coverage_fraction": coverage_fractions,
        "minimum_common_date_coverage_fraction": minimum_fraction,
        "candidate_active_observation_count": candidate_active_count,
        "wp20_minimum_aligned_observations": uniqueness_policy[
            "minimum_aligned_observations"],
        "wp20_minimum_candidate_active_observations": uniqueness_policy[
            "minimum_candidate_active_observations"],
        "wp20_minimum_common_date_coverage_fraction": uniqueness_policy[
            "minimum_common_date_coverage_fraction"],
        "performance_metrics_evaluated": False,
        "profitability_established": False,
    }
    return preflight, sorted(set(blockers))


def build_cohort(events: list[dict], candidate_strategy_id: str, registry_policy: dict,
                 uniqueness_policy: dict, repository_root: Path,
                 generated_at_utc: str) -> dict:
    registry_errors = sr.validate_events(events, registry_policy)
    uniqueness_errors = su.validate_policy(uniqueness_policy)
    if registry_errors or uniqueness_errors:
        return _refuse("cohort_policy_or_registry_invalid",
                       errors=[*registry_errors, *uniqueness_errors])
    generated = sr.parse_utc(generated_at_utc)
    if generated is None:
        return _refuse("generated_at_utc_invalid")
    registrations = [
        event for event in events
        if event["event_type"] == "register_strategy"
        and event["payload"]["strategy_id"] == candidate_strategy_id
    ]
    if len(registrations) != 1:
        return _refuse("candidate_registration_not_unique")
    candidate_event = registrations[0]
    if candidate_event["payload"]["role"] != "candidate_review":
        return _refuse("candidate_was_not_registered_for_review")
    if generated < sr.parse_utc(candidate_event["occurred_at_utc"]):
        return _refuse("cohort_generated_before_candidate_lock")
    lock_sequence = candidate_event["sequence"]
    prefix = events[:lock_sequence + 1]
    states = sr.replay(prefix)
    candidate_state = states[candidate_strategy_id]
    decisions = [
        _selection_decision(states[strategy_id], candidate_strategy_id)
        for strategy_id in sorted(states)
    ]
    benchmarks = sorted(
        (state for state in states.values() if state["role"] == "benchmark_reference"),
        key=lambda value: value["strategy_id"],
    )
    incumbents = sorted(
        (state for state in states.values() if state["role"] == "incumbent_reference"),
        key=lambda value: value["strategy_id"],
    )
    blockers: list[str] = []
    if len(benchmarks) != 1:
        blockers.append(f"benchmark_reference_count_not_one:{len(benchmarks)}")
    if len(incumbents) < registry_policy["minimum_incumbents"]:
        blockers.append(f"incumbent_count_below_minimum:{len(incumbents)}")
    if len(incumbents) > registry_policy["maximum_incumbents"]:
        blockers.append(f"incumbent_count_above_maximum:{len(incumbents)}")
    selected = {
        "candidate": _source_ref(candidate_state),
        "benchmarks": [_source_ref(state) for state in benchmarks],
        "incumbents": [_source_ref(state) for state in incumbents],
    }
    documents: dict[str, dict] = {}
    if len(benchmarks) == 1 and registry_policy["minimum_incumbents"] <= len(
            incumbents) <= registry_policy["maximum_incumbents"]:
        documents, source_blockers = _load_selected_sources(
            selected, repository_root, registry_policy)
        blockers.extend(source_blockers)
    preflight, blockers = _preflight(selected, documents, uniqueness_policy, blockers)
    cohort_id = f"cohort:{sr.sha256_obj(candidate_strategy_id)[:20]}:seq-{lock_sequence}"
    cohort = {
        **SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": COHORT_SCHEMA,
        "cohort_id": cohort_id,
        "candidate_strategy_id": candidate_strategy_id,
        "generated_at_utc": generated_at_utc,
        "status": "ready" if not blockers else "blocked",
        "registry_lock": {
            "registry_id": registry_policy["registry_id"],
            "candidate_registration_sequence": lock_sequence,
            "candidate_registration_time_utc": candidate_event["occurred_at_utc"],
            "candidate_registration_event_sha256": candidate_event["event_sha256"],
            "event_count_through_lock": len(prefix),
            "prefix_sha256": sr.sha256_obj(prefix),
            "head_event_sha256": prefix[-1]["event_sha256"],
        },
        "policy": {
            "registry_policy_version": registry_policy["version"],
            "registry_policy_sha256": sr.sha256_obj(registry_policy),
            "uniqueness_policy_version": uniqueness_policy["version"],
            "uniqueness_policy_sha256": su.sha256_obj(uniqueness_policy),
        },
        "selection_decisions": decisions,
        "selected_sources": selected,
        "comparison_preflight": preflight,
        "blockers": blockers,
        "actual_orders_count": 0,
        "cohort_sha256": "",
    }
    cohort["cohort_sha256"] = sr.sha256_obj(_payload(cohort, "cohort_sha256"))
    errors = validate_cohort(cohort)
    return cohort if not errors else _refuse("cohort_self_validation_failed", errors=errors)


def _validate_source_ref(reference: Any, role: str, tag: str) -> list[str]:
    if not isinstance(reference, dict) or set(reference) != _SOURCE_REF_KEYS:
        return [f"cohort_source_ref_invalid:{tag}"]
    errors = []
    if reference.get("role") != role:
        errors.append(f"cohort_source_role_invalid:{tag}")
    if not sr._ID_RE.fullmatch(reference.get("strategy_id", "")):
        errors.append(f"cohort_source_id_invalid:{tag}")
    for key in ("registration_event_sha256", "source_result_sha256",
                "source_content_sha256", "cost_policy_sha256", "portfolio_policy_sha256"):
        if not sr._SHA_RE.fullmatch(reference.get(key, "")):
            errors.append(f"cohort_source_hash_invalid:{tag}:{key}")
    return errors


def validate_cohort(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["cohort_not_object"]
    errors: list[str] = []
    if set(document) != _COHORT_KEYS:
        errors.append("cohort_top_level_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"cohort_safety_invalid:{key}")
    if (document.get("tool") != TOOL or document.get("ok") is not True
            or document.get("schema") != COHORT_SCHEMA):
        errors.append("cohort_identity_invalid")
    if not sr._ID_RE.fullmatch(document.get("cohort_id", "")):
        errors.append("cohort_id_invalid")
    if not sr._ID_RE.fullmatch(document.get("candidate_strategy_id", "")):
        errors.append("cohort_candidate_id_invalid")
    if sr.parse_utc(document.get("generated_at_utc")) is None:
        errors.append("cohort_generated_time_invalid")
    if document.get("status") not in {"ready", "blocked"}:
        errors.append("cohort_status_invalid")
    blockers = document.get("blockers")
    if not isinstance(blockers, list) or blockers != sorted(set(blockers)) or any(
            not isinstance(value, str) or not value for value in blockers):
        errors.append("cohort_blockers_invalid")
        blockers = []
    if (document.get("status") == "ready") != (not blockers):
        errors.append("cohort_status_blocker_mismatch")
    lock = document.get("registry_lock")
    if not isinstance(lock, dict):
        errors.append("cohort_registry_lock_invalid")
    else:
        if lock.get("event_count_through_lock") != lock.get(
                "candidate_registration_sequence", -2) + 1:
            errors.append("cohort_registry_lock_count_mismatch")
        for key in ("candidate_registration_event_sha256", "prefix_sha256",
                    "head_event_sha256"):
            if not sr._SHA_RE.fullmatch(lock.get(key, "")):
                errors.append(f"cohort_registry_lock_hash_invalid:{key}")
        if lock.get("head_event_sha256") != lock.get("candidate_registration_event_sha256"):
            errors.append("cohort_registry_lock_head_mismatch")
    policy = document.get("policy")
    if not isinstance(policy, dict) or set(policy) != {
            "registry_policy_version", "registry_policy_sha256",
            "uniqueness_policy_version", "uniqueness_policy_sha256"}:
        errors.append("cohort_policy_binding_invalid")
    decisions = document.get("selection_decisions")
    if not isinstance(decisions, list) or any(
            not isinstance(row, dict) or set(row) != _DECISION_KEYS for row in decisions):
        errors.append("cohort_selection_decisions_invalid")
    elif [row["strategy_id"] for row in decisions] != sorted(
            row["strategy_id"] for row in decisions):
        errors.append("cohort_selection_decisions_not_sorted")
    selected = document.get("selected_sources")
    if not isinstance(selected, dict) or set(selected) != {
            "candidate", "benchmarks", "incumbents"}:
        errors.append("cohort_selected_sources_invalid")
    else:
        errors.extend(_validate_source_ref(selected.get("candidate"), "candidate_review",
                                           "candidate"))
        for key, role in (("benchmarks", "benchmark_reference"),
                          ("incumbents", "incumbent_reference")):
            rows = selected.get(key)
            if not isinstance(rows, list):
                errors.append(f"cohort_selected_list_invalid:{key}")
                continue
            ids = []
            for index, row in enumerate(rows):
                errors.extend(_validate_source_ref(row, role, f"{key}:{index}"))
                if isinstance(row, dict):
                    ids.append(row.get("strategy_id"))
            if ids != sorted(ids):
                errors.append(f"cohort_selected_not_sorted:{key}")
    if not isinstance(document.get("comparison_preflight"), dict):
        errors.append("cohort_preflight_invalid")
    if document.get("actual_orders_count") != 0:
        errors.append("cohort_claims_actual_orders")
    if document.get("cohort_sha256") != sr.sha256_obj(_payload(document, "cohort_sha256")):
        errors.append("cohort_sha256_mismatch")
    return sorted(set(errors))


def verify_cohort(document: dict, events: list[dict], registry_policy: dict,
                  uniqueness_policy: dict, repository_root: Path) -> list[str]:
    errors = validate_cohort(document)
    if errors:
        return errors
    lock_count = document["registry_lock"]["event_count_through_lock"]
    if len(events) < lock_count:
        return ["cohort_registry_prefix_missing"]
    prefix = events[:lock_count]
    if sr.sha256_obj(prefix) != document["registry_lock"]["prefix_sha256"]:
        return ["cohort_registry_prefix_drift"]
    rebuilt = build_cohort(
        events,
        document["candidate_strategy_id"],
        registry_policy,
        uniqueness_policy,
        repository_root,
        document["generated_at_utc"],
    )
    if not rebuilt.get("ok"):
        return ["cohort_rebuild_failed", *rebuilt.get("errors", [])]
    if rebuilt != document:
        return ["cohort_semantic_rebuild_mismatch"]
    return []


def build_audit_wrapper(cohort: dict, events: list[dict], registry_policy: dict,
                        uniqueness_policy: dict, repository_root: Path) -> dict:
    errors = verify_cohort(cohort, events, registry_policy, uniqueness_policy, repository_root)
    if errors:
        return _refuse("cohort_verification_failed", errors=errors)
    if cohort["status"] != "ready":
        return _refuse("cohort_not_ready", blockers=cohort["blockers"])
    selected = cohort["selected_sources"]
    documents, blockers = _load_selected_sources(selected, repository_root, registry_policy)
    if blockers:
        return _refuse("cohort_sources_changed", blockers=blockers)
    candidate_ref = selected["candidate"]
    benchmark_ref = selected["benchmarks"][0]
    incumbent_refs = selected["incumbents"]
    uniqueness = su.audit_portfolios(
        documents[candidate_ref["strategy_id"]],
        documents[benchmark_ref["strategy_id"]],
        {reference["strategy_id"]: documents[reference["strategy_id"]]
         for reference in incumbent_refs},
        uniqueness_policy,
        cohort["generated_at_utc"],
        candidate_ref["strategy_id"],
        benchmark_ref["strategy_id"],
    )
    if not uniqueness.get("ok"):
        return _refuse("uniqueness_audit_failed", detail=uniqueness.get("error"),
                       errors=uniqueness.get("errors", []))
    wrapper = {
        **SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": AUDIT_SCHEMA,
        "cohort_id": cohort["cohort_id"],
        "cohort_sha256": cohort["cohort_sha256"],
        "candidate_strategy_id": cohort["candidate_strategy_id"],
        "registry_lock_head_sha256": cohort["registry_lock"]["head_event_sha256"],
        "classification": uniqueness["classification"],
        "uniqueness_result": uniqueness,
        "uniqueness_result_sha256": uniqueness["result_sha256"],
        "actual_orders_count": 0,
        "audit_sha256": "",
    }
    wrapper["audit_sha256"] = sr.sha256_obj(_payload(wrapper, "audit_sha256"))
    wrapper_errors = validate_audit_wrapper(wrapper)
    return wrapper if not wrapper_errors else _refuse(
        "cohort_audit_self_validation_failed", errors=wrapper_errors)


def validate_audit_wrapper(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["cohort_audit_not_object"]
    errors = []
    if set(document) != _AUDIT_KEYS:
        errors.append("cohort_audit_top_level_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"cohort_audit_safety_invalid:{key}")
    if (document.get("tool") != TOOL or document.get("ok") is not True
            or document.get("schema") != AUDIT_SCHEMA):
        errors.append("cohort_audit_identity_invalid")
    if (not sr._ID_RE.fullmatch(document.get("cohort_id", ""))
            or not sr._ID_RE.fullmatch(document.get("candidate_strategy_id", ""))):
        errors.append("cohort_audit_id_invalid")
    uniqueness = document.get("uniqueness_result")
    uniqueness_errors = su.validate_result(uniqueness)
    errors.extend(f"nested_{value}" for value in uniqueness_errors)
    if isinstance(uniqueness, dict):
        if document.get("uniqueness_result_sha256") != uniqueness.get("result_sha256"):
            errors.append("cohort_audit_uniqueness_hash_mismatch")
        if document.get("classification") != uniqueness.get("classification"):
            errors.append("cohort_audit_classification_mismatch")
    for key in ("cohort_sha256", "registry_lock_head_sha256",
                "uniqueness_result_sha256"):
        if not sr._SHA_RE.fullmatch(document.get(key, "")):
            errors.append(f"cohort_audit_hash_invalid:{key}")
    if document.get("actual_orders_count") != 0:
        errors.append("cohort_audit_claims_actual_orders")
    if document.get("audit_sha256") != sr.sha256_obj(_payload(document, "audit_sha256")):
        errors.append("cohort_audit_sha256_mismatch")
    return sorted(set(errors))


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > sr._MAX_BYTES:
        raise ValueError("input_not_regular_bounded_file")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frozen strategy comparison cohorts.")
    parser.add_argument("--registry-policy", default=str(sr.POLICY_DEFAULT))
    parser.add_argument("--uniqueness-policy", default=str(su.POLICY_DEFAULT))
    parser.add_argument("--registry", default=str(sr.REGISTRY_DEFAULT))
    parser.add_argument("--repo-root", default=str(ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--candidate-id", required=True)
    build.add_argument("--generated-at", required=True)
    build.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--cohort", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--cohort", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--cohort", required=True)
    audit.add_argument("--out", required=True)
    validate_audit = sub.add_parser("validate-audit")
    validate_audit.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    registry_policy, registry_policy_errors = sr.load_policy(Path(args.registry_policy))
    uniqueness_policy, uniqueness_policy_errors = su.load_policy(Path(args.uniqueness_policy))
    if (registry_policy is None or registry_policy_errors or uniqueness_policy is None
            or uniqueness_policy_errors):
        output = _refuse("cohort_policy_invalid",
                         errors=[*registry_policy_errors, *uniqueness_policy_errors])
    else:
        try:
            if args.command == "validate":
                document = _load_json(Path(args.cohort))
                errors = validate_cohort(document)
                output = {**SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                          "status": document.get("status"),
                          "cohort_sha256": document.get("cohort_sha256")}
            elif args.command == "validate-audit":
                document = _load_json(Path(args.artifact))
                errors = validate_audit_wrapper(document)
                output = {**SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                          "classification": document.get("classification"),
                          "audit_sha256": document.get("audit_sha256")}
            else:
                events, registry_errors = sr.read_registry(Path(args.registry), registry_policy)
                if registry_errors:
                    output = _refuse("registry_invalid", errors=registry_errors)
                elif args.command == "build":
                    output = build_cohort(
                        events, args.candidate_id, registry_policy, uniqueness_policy,
                        Path(args.repo_root), args.generated_at)
                    if output.get("ok"):
                        _atomic_write_once(Path(args.out), output)
                else:
                    cohort = _load_json(Path(args.cohort))
                    if args.command == "verify":
                        errors = verify_cohort(
                            cohort, events, registry_policy, uniqueness_policy,
                            Path(args.repo_root))
                        output = {**SAFETY, "tool": TOOL, "ok": not errors,
                                  "errors": errors, "status": cohort.get("status"),
                                  "cohort_sha256": cohort.get("cohort_sha256")}
                    else:
                        output = build_audit_wrapper(
                            cohort, events, registry_policy, uniqueness_policy,
                            Path(args.repo_root))
                        if output.get("ok"):
                            _atomic_write_once(Path(args.out), output)
        except Exception as exc:
            output = _refuse(f"{type(exc).__name__}:{exc}")
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
