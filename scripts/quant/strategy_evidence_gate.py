#!/usr/bin/env python3
"""Fail-closed strategy evidence gate for human shadow-observation review.

This component joins the existing factory, staged-package, cohort uniqueness,
dependence-aware inference, and SPA contracts. Its strongest terminal only says that a
human may review whether to begin a separate shadow observation. It never registers,
promotes, activates, or executes a strategy.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

from quant import inference_audit as ia  # noqa: E402
from quant import portfolio_backtest as pb  # noqa: E402
from quant import research_factory as rf  # noqa: E402
from quant import spa_audit as spa  # noqa: E402
from quant import strategy_artifact_stager as stager  # noqa: E402
from quant import strategy_artifacts as sa  # noqa: E402
from quant import strategy_cohort as sc  # noqa: E402
from quant import strategy_uniqueness as su  # noqa: E402

TOOL = "quant.strategy_evidence_gate"
POLICY_SCHEMA = "quant_strategy_evidence_gate_policy/1.0"
RESULT_SCHEMA = "quant_strategy_evidence_gate/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_evidence_gate.json"
FACTORY_CONFIG_DEFAULT = rf.CONFIG_DEFAULT
ARTIFACT_POLICY_DEFAULT = sa.POLICY_DEFAULT
INFERENCE_POLICY_DEFAULT = ia.POLICY_DEFAULT
UNIQUENESS_POLICY_DEFAULT = su.POLICY_DEFAULT
SPA_POLICY_DEFAULT = spa.DEFAULT_POLICY
_MAX_BYTES = 128 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_RE = re.compile(r"^postclose-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_NUM = (int, float)

SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "feeds_production": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "shadow_observation_start_allowed": False,
    "registry_mutation_allowed": False,
    "broker_access": "none",
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
    "model_invoked": False,
}

REQUIRED_FACTORY_GATES = [
    "complete_trial_matrix",
    "deflated_sharpe_probability",
    "locked_chronological_holdout",
    "maximum_holdout_drawdown",
    "maximum_probability_backtest_overfitting",
    "minimum_completed_oos",
    "minimum_fold_expectancy",
    "minimum_holdout_completed",
    "minimum_holdout_profit_factor",
    "minimum_walk_forward_folds",
    "positive_expectancy_under_cost_stress",
    "positive_fold_fraction",
    "positive_holdout_expectancy_after_costs",
]

POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "version": "1.0.0",
    "strongest_terminal": "eligible_for_shadow_observation_review",
    "allowed_input_roots": [
        "data/runtime/experiments",
        "data/runtime/health",
        "data/runtime/quant",
    ],
    "required_stager_statuses": ["staged", "verified_existing"],
    "required_factory_status": "candidate_for_category_D_governance_review",
    "required_factory_gate_codes": REQUIRED_FACTORY_GATES,
    "required_uniqueness_classification": "incremental_alpha_review_candidate",
    "required_inference_status": "reviewable",
    "required_spa_status": "reviewable",
    "require_spa_family_complete": True,
    "require_spa_robust_across_configured_block_lengths": True,
    "require_candidate_best_sample_model_in_every_configuration": True,
    "require_exact_candidate_hash_binding": True,
    "require_exact_benchmark_hash_binding": True,
    "require_current_run_factory_file_binding": True,
    "require_revalidated_staged_package": True,
    "classification_order": [
        "blocked",
        "not_eligible",
        "eligible_for_shadow_observation_review",
    ],
    "safety": SAFETY,
}

POLICY_KEYS = {
    *POLICY_FIXED,
    "objective",
    "minimum_cost_stress_multiplier",
    "minimum_inference_daily_observations",
    "minimum_effective_observations",
    "minimum_effective_observation_fraction",
    "minimum_hac_lower_confidence_bound",
    "minimum_bootstrap_lower_confidence_bound",
    "minimum_candidate_spa_mean_return_differential",
}

LIMITATIONS = [
    "All inputs are historical hypothetical research artifacts after modeled costs, not realized account performance.",
    "Passing every gate does not establish future profitability, capacity, fill quality, or causal alpha.",
    "HAC, bootstrap, DSR, PBO, and SPA rely on assumptions that may fail under structural breaks or undisclosed model search.",
    "The incumbent registry can only compare strategies that were completely and honestly registered before the cohort lock.",
    "Shadow-observation review eligibility does not start a shadow run, change production, create an order, or grant activation authority.",
    "Any later production change requires a separate evidence review, explicit approval, protected patch, and unchanged risk and execution controls.",
]

RESULT_KEYS = {
    *SAFETY,
    "schema",
    "tool",
    "ok",
    "generated_utc",
    "policy_version",
    "policy_sha256",
    "target_session_date",
    "post_close_run_id",
    "candidate_strategy_id",
    "candidate_portfolio_id",
    "benchmark_strategy_id",
    "benchmark_portfolio_id",
    "status",
    "input_artifacts",
    "bindings",
    "evidence_snapshot",
    "evidence_checks",
    "passed_check_count",
    "failed_check_codes",
    "blockers",
    "limitations",
    "eligible_for_shadow_observation_review",
    "shadow_observation_started",
    "human_approval_required",
    "production_change_required",
    "profitability_status",
    "actual_orders_count",
    "result_sha256",
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


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _refuse(error: str, **extra: Any) -> dict:
    return {**SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["evidence_gate_policy_not_object"]
    errors: list[str] = []
    if set(document) != POLICY_KEYS:
        errors.append("evidence_gate_policy_keys_invalid")
    for key, expected in POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"evidence_gate_policy_rule_invalid:{key}")
    if not isinstance(document.get("objective"), str) or not document.get(
            "objective", "").strip():
        errors.append("evidence_gate_policy_objective_invalid")
    integer_minima = {
        "minimum_inference_daily_observations": 60,
        "minimum_effective_observations": 30,
    }
    for key, minimum in integer_minima.items():
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            errors.append(f"evidence_gate_policy_minimum_too_weak:{key}")
    numeric_minima = {
        "minimum_cost_stress_multiplier": 2.0,
        "minimum_effective_observation_fraction": 0.25,
        "minimum_hac_lower_confidence_bound": 0.0,
        "minimum_bootstrap_lower_confidence_bound": 0.0,
        "minimum_candidate_spa_mean_return_differential": 0.0,
    }
    for key, minimum in numeric_minima.items():
        value = document.get(key)
        if not _number(value) or float(value) < minimum:
            errors.append(f"evidence_gate_policy_minimum_too_weak:{key}")
    fraction = document.get("minimum_effective_observation_fraction")
    if _number(fraction) and float(fraction) > 1:
        errors.append("evidence_gate_policy_effective_fraction_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise ValueError("policy_not_regular_bounded_file")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, ["evidence_gate_policy_unreadable"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    return {
        **SAFETY,
        "tool": TOOL,
        "ok": policy is not None and not errors,
        "valid": policy is not None and not errors,
        "errors": errors,
        "policy_sha256": sha256_obj(policy) if policy is not None else None,
        "strongest_terminal": policy.get("strongest_terminal") if policy else None,
    }


def _portable(path: Path, repository_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_runtime(value: str | Path, repository_root: Path, policy: dict,
                     *, directory: bool = False) -> Path:
    path = Path(value).expanduser()
    candidate = path if path.is_absolute() else repository_root / path
    if candidate.is_symlink():
        raise ValueError("runtime_path_symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("runtime_path_missing") from exc
    if directory and not resolved.is_dir():
        raise ValueError("runtime_path_not_directory")
    if not directory and not resolved.is_file():
        raise ValueError("runtime_path_not_file")
    allowed = False
    for relative in policy["allowed_input_roots"]:
        root = (repository_root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        allowed = True
        break
    if not allowed:
        raise ValueError("runtime_path_outside_allowlist")
    if not directory and resolved.stat().st_size > _MAX_BYTES:
        raise ValueError("runtime_file_too_large")
    return resolved


def _read_runtime(value: str | Path, repository_root: Path,
                  policy: dict) -> tuple[Path, dict]:
    path = _resolve_runtime(value, repository_root, policy)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime_file_unreadable:{type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise ValueError("runtime_document_not_object")
    return path, document


def _input_ref(role: str, path: Path, document: dict, semantic_key: str | None,
               repository_root: Path) -> dict:
    semantic = document.get(semantic_key) if semantic_key else None
    return {
        "role": role,
        "path": _portable(path, repository_root),
        "file_sha256": sha256_file(path),
        "schema": document.get("schema") if isinstance(document.get("schema"), str) else None,
        "semantic_sha256": semantic if isinstance(semantic, str) and _SHA_RE.fullmatch(
            semantic) else None,
    }


def _load_contract(path: Path, loader: Any) -> tuple[dict | None, list[str]]:
    document, errors = loader(path)
    return document, list(errors)


def _package_documents(stager_doc: dict, repository_root: Path, policy: dict,
                       artifact_policy: dict) -> tuple[dict | None, dict | None,
                                                       dict | None, list[dict], list[str]]:
    try:
        package = _resolve_runtime(stager_doc.get("package_path", ""), repository_root,
                                   policy, directory=True)
    except (TypeError, ValueError) as exc:
        return None, None, None, [], [f"staged_package:{exc}"]
    expected = {
        "candidate_portfolio.json",
        "spy_benchmark_reference.json",
        "export_receipt.json",
    }
    try:
        names = {path.name for path in package.iterdir()}
    except OSError as exc:
        return None, None, None, [], [f"staged_package_unreadable:{type(exc).__name__}"]
    if names != expected:
        return None, None, None, [], ["staged_package_file_set_invalid"]
    loaded: dict[str, tuple[Path, dict]] = {}
    blockers: list[str] = []
    for name in sorted(expected):
        try:
            loaded[name] = _read_runtime(package / name, repository_root, policy)
        except ValueError as exc:
            blockers.append(f"staged_package:{name}:{exc}")
    if blockers:
        return None, None, None, [], blockers
    candidate_path, candidate = loaded["candidate_portfolio.json"]
    benchmark_path, benchmark = loaded["spy_benchmark_reference.json"]
    receipt_path, receipt = loaded["export_receipt.json"]
    blockers.extend(f"staged_candidate:{error}" for error in pb.validate_result(candidate))
    blockers.extend(f"staged_benchmark:{error}" for error in sa.validate_benchmark(
        benchmark, artifact_policy))
    blockers.extend(f"staged_receipt:{error}" for error in sa.validate_receipt(receipt))
    exported = receipt.get("exported_artifacts") if isinstance(
        receipt.get("exported_artifacts"), dict) else {}
    candidate_ref = exported.get("candidate_portfolio") if isinstance(
        exported.get("candidate_portfolio"), dict) else {}
    benchmark_ref = exported.get("benchmark_reference") if isinstance(
        exported.get("benchmark_reference"), dict) else {}
    receipt_sources = receipt.get("source_artifacts") if isinstance(
        receipt.get("source_artifacts"), dict) else {}
    receipt_candidate_ref = receipt_sources.get("candidate_portfolio") if isinstance(
        receipt_sources.get("candidate_portfolio"), dict) else {}
    comparisons = {
        "staged_receipt_status_not_exported": (
            receipt.get("status"), "exported"),
        "staged_candidate_file_sha256_mismatch": (
            candidate_ref.get("sha256"), sha256_file(candidate_path)),
        "staged_candidate_result_sha256_mismatch": (
            candidate_ref.get("result_sha256"), candidate.get("result_sha256")),
        "staged_benchmark_file_sha256_mismatch": (
            benchmark_ref.get("sha256"), sha256_file(benchmark_path)),
        "staged_benchmark_result_sha256_mismatch": (
            benchmark_ref.get("result_sha256"), benchmark.get("result_sha256")),
        "stager_candidate_result_sha256_mismatch": (
            stager_doc.get("candidate_result_sha256"), candidate.get("result_sha256")),
        "stager_benchmark_result_sha256_mismatch": (
            stager_doc.get("benchmark_result_sha256"), benchmark.get("result_sha256")),
        "stager_export_receipt_sha256_mismatch": (
            stager_doc.get("export_receipt_sha256"), receipt.get("receipt_sha256")),
        "stager_bundle_sha256_mismatch": (
            stager_doc.get("bundle_sha256"), receipt.get("bundle_sha256")),
        "receipt_source_candidate_file_sha256_mismatch": (
            receipt_candidate_ref.get("sha256"), sha256_file(candidate_path)),
        "receipt_source_candidate_cost_stress_mismatch": (
            receipt_sources.get("cost_stress_multiplier"),
            candidate.get("cost_stress_multiplier")),
    }
    blockers.extend(code for code, values in comparisons.items() if values[0] != values[1])
    refs = [
        _input_ref("candidate_portfolio", candidate_path, candidate, "result_sha256",
                   repository_root),
        _input_ref("benchmark_portfolio", benchmark_path, benchmark, "result_sha256",
                   repository_root),
        _input_ref("export_receipt", receipt_path, receipt, "receipt_sha256",
                   repository_root),
    ]
    return candidate, benchmark, receipt, refs, sorted(set(blockers))


def _verify_spa_sources(panel: dict, repository_root: Path,
                        policy: dict) -> tuple[list[dict], list[str]]:
    refs: list[dict] = []
    blockers: list[str] = []
    sources = [("spa_benchmark_source", panel.get("benchmark"))]
    sources.extend((f"spa_model_source:{index}", source)
                   for index, source in enumerate(panel.get("models", [])))
    seen_paths: set[str] = set()
    for role, source in sources:
        if not isinstance(source, dict):
            blockers.append(f"{role}:source_not_object")
            continue
        try:
            path, document = _read_runtime(source.get("source_path", ""),
                                           repository_root, policy)
        except (TypeError, ValueError) as exc:
            blockers.append(f"{role}:{exc}")
            continue
        portable = _portable(path, repository_root)
        if portable in seen_paths:
            blockers.append(f"{role}:source_path_reused")
        seen_paths.add(portable)
        blockers.extend(f"{role}:{error}" for error in pb.validate_result(document))
        comparisons = {
            "file_sha256_mismatch": (source.get("source_file_sha256"), sha256_file(path)),
            "result_sha256_mismatch": (
                source.get("source_result_sha256"), document.get("result_sha256")),
            "daily_path_sha256_mismatch": (
                source.get("source_daily_path_sha256"), document.get("daily_path_sha256")),
            "portfolio_id_mismatch": (
                source.get("portfolio_id"), document.get("variant_id")),
        }
        blockers.extend(f"{role}:{code}" for code, values in comparisons.items()
                        if values[0] != values[1])
        refs.append(_input_ref(role, path, document, "result_sha256", repository_root))
    return refs, sorted(set(blockers))


def _factory_experiment(factory: dict, bundle_sha256: Any) -> tuple[dict | None, list[str]]:
    matches = [row for row in factory.get("experiments", [])
               if isinstance(row, dict)
               and isinstance(row.get("metrics"), dict)
               and row["metrics"].get("bundle_sha256") == bundle_sha256]
    if len(matches) != 1:
        return None, ["factory_current_bundle_match_count_invalid"]
    return matches[0], []


def _check(code: str, category: str, passed: bool, observed: Any,
           required: Any) -> dict:
    return {
        "code": code,
        "category": category,
        "passed": bool(passed),
        "observed": observed,
        "required": required,
    }


def _strictly_above(value: Any, threshold: Any) -> bool:
    return _number(value) and _number(threshold) and float(value) > float(threshold)


def _base(generated_utc: str, policy: dict) -> dict:
    return {
        **SAFETY,
        "schema": RESULT_SCHEMA,
        "tool": TOOL,
        "ok": True,
        "generated_utc": generated_utc,
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "target_session_date": None,
        "post_close_run_id": None,
        "candidate_strategy_id": None,
        "candidate_portfolio_id": None,
        "benchmark_strategy_id": None,
        "benchmark_portfolio_id": None,
        "status": "blocked",
        "input_artifacts": [],
        "bindings": {},
        "evidence_snapshot": {},
        "evidence_checks": [],
        "passed_check_count": 0,
        "failed_check_codes": [],
        "blockers": [],
        "limitations": LIMITATIONS,
        "eligible_for_shadow_observation_review": False,
        "shadow_observation_started": False,
        "human_approval_required": True,
        "production_change_required": True,
        "profitability_status": "not_established",
        "actual_orders_count": 0,
        "result_sha256": "",
    }


def _finish(document: dict) -> dict:
    blockers = sorted(set(document.get("blockers", [])))
    checks = document.get("evidence_checks", [])
    failed = [row["code"] for row in checks if row.get("passed") is not True]
    document["blockers"] = blockers
    document["failed_check_codes"] = failed
    document["passed_check_count"] = sum(row.get("passed") is True for row in checks)
    if blockers:
        status = "blocked"
    elif failed:
        status = "not_eligible"
    else:
        status = "eligible_for_shadow_observation_review"
    document["status"] = status
    document["eligible_for_shadow_observation_review"] = (
        status == "eligible_for_shadow_observation_review")
    document["result_sha256"] = sha256_obj(_payload(document, "result_sha256"))
    return document


def run_gate(*, stager_path: Path, factory_path: Path, cohort_audit_path: Path,
             inference_audit_path: Path, spa_panel_path: Path, spa_audit_path: Path,
             repository_root: Path = ROOT, policy_path: Path = POLICY_DEFAULT,
             factory_config_path: Path = FACTORY_CONFIG_DEFAULT,
             artifact_policy_path: Path = ARTIFACT_POLICY_DEFAULT,
             inference_policy_path: Path = INFERENCE_POLICY_DEFAULT,
             uniqueness_policy_path: Path = UNIQUENESS_POLICY_DEFAULT,
             spa_policy_path: Path = SPA_POLICY_DEFAULT,
             generated_utc: str | None = None) -> tuple[dict, int]:
    policy, policy_errors = load_policy(policy_path)
    if policy is None or policy_errors:
        return _refuse("evidence_gate_policy_invalid", errors=policy_errors), 1
    generated = _parse_utc(generated_utc) if generated_utc else dt.datetime.now(
        dt.timezone.utc)
    if generated is None:
        return _refuse("generated_utc_invalid"), 1
    result = _base(_utc(generated), policy)
    blockers: list[str] = []
    refs: list[dict] = []

    source_specs = [
        ("stager", stager_path, "health_sha256", stager.validate_health),
        ("factory", factory_path, "artifact_sha256", rf.validate_artifact),
        ("cohort_audit", cohort_audit_path, "audit_sha256", sc.validate_audit_wrapper),
        ("inference_audit", inference_audit_path, "audit_sha256", ia.validate_inference),
        ("spa_panel", spa_panel_path, "panel_sha256",
         lambda value: spa.validate_panel(value, rebuild=False)),
        ("spa_audit", spa_audit_path, "audit_sha256",
         lambda value: spa.validate_audit(value, rebuild=False)),
    ]
    documents: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    for role, path, semantic_key, validator in source_specs:
        try:
            resolved, document = _read_runtime(path, repository_root, policy)
        except ValueError as exc:
            blockers.append(f"{role}:{exc}")
            continue
        paths[role] = resolved
        documents[role] = document
        refs.append(_input_ref(role, resolved, document, semantic_key, repository_root))
        try:
            errors = validator(document)
        except Exception as exc:  # deterministic validator failure is a hard block
            errors = [f"validator_exception:{type(exc).__name__}"]
        blockers.extend(f"{role}:{error}" for error in errors)

    contract_loaders = [
        ("factory_config", factory_config_path, rf.load_config),
        ("artifact_policy", artifact_policy_path, sa.load_policy),
        ("inference_policy", inference_policy_path, ia.load_policy),
        ("uniqueness_policy", uniqueness_policy_path, su.load_policy),
    ]
    contracts: dict[str, dict] = {}
    for name, path, loader in contract_loaders:
        document, errors = _load_contract(path, loader)
        if document is None or errors:
            blockers.extend(f"{name}:{error}" for error in (errors or ["missing"]))
        else:
            contracts[name] = document
    try:
        spa_policy, spa_policy_sha = spa.load_policy(spa_policy_path)
        contracts["spa_policy"] = spa_policy
    except Exception as exc:
        spa_policy_sha = None
        blockers.append(f"spa_policy:{type(exc).__name__}")

    if set(documents) != {row[0] for row in source_specs}:
        result["input_artifacts"] = sorted(refs, key=lambda row: row["role"])
        result["blockers"] = blockers or ["required_input_missing"]
        return _finish(result), 1
    if set(contracts) != {
            "factory_config", "artifact_policy", "inference_policy",
            "uniqueness_policy", "spa_policy"}:
        result["input_artifacts"] = sorted(refs, key=lambda row: row["role"])
        result["blockers"] = blockers or ["required_contract_missing"]
        return _finish(result), 1

    stager_doc = documents["stager"]
    factory = documents["factory"]
    cohort = documents["cohort_audit"]
    inference = documents["inference_audit"]
    panel = documents["spa_panel"]
    spa_result = documents["spa_audit"]
    uniqueness = cohort.get("uniqueness_result", {})
    result["target_session_date"] = stager_doc.get("target_session_date")
    result["post_close_run_id"] = stager_doc.get("post_close_run_id")
    result["candidate_strategy_id"] = cohort.get("candidate_strategy_id")

    if factory.get("target_session_date") != stager_doc.get("target_session_date"):
        blockers.append("current_run_target_session_mismatch")
    if factory.get("post_close_run_id") != stager_doc.get("post_close_run_id"):
        blockers.append("current_run_id_mismatch")
    if sha256_file(paths["factory"]) != stager_doc.get("factory_health_file_sha256"):
        blockers.append("current_run_factory_file_sha256_mismatch")
    if sha256_file(factory_config_path) != factory.get("config_sha256"):
        blockers.append("factory_config_sha256_mismatch")
    if uniqueness.get("policy_sha256") != su.sha256_obj(contracts["uniqueness_policy"]):
        blockers.append("uniqueness_policy_sha256_mismatch")
    if inference.get("policy_sha256") != ia.sha256_obj(contracts["inference_policy"]):
        blockers.append("inference_policy_sha256_mismatch")
    if spa_result.get("policy_sha256") != spa_policy_sha:
        blockers.append("spa_policy_sha256_mismatch")

    try:
        audit_panel_path = _resolve_runtime(spa_result.get("source_panel_path", ""),
                                            repository_root, policy)
        if audit_panel_path != paths["spa_panel"]:
            blockers.append("spa_audit_panel_path_mismatch")
    except (TypeError, ValueError) as exc:
        blockers.append(f"spa_audit_panel_path:{exc}")
    if spa_result.get("source_panel_file_sha256") != sha256_file(paths["spa_panel"]):
        blockers.append("spa_audit_panel_file_sha256_mismatch")
    if spa_result.get("source_panel_sha256") != panel.get("panel_sha256"):
        blockers.append("spa_audit_panel_semantic_sha256_mismatch")
    if spa_result.get("comparison_id") != panel.get("comparison_id"):
        blockers.append("spa_audit_comparison_id_mismatch")
    try:
        expected_spa_status, expected_spa_analysis = spa._analysis(
            panel, contracts["spa_policy"])
        if spa_result.get("status") != expected_spa_status:
            blockers.append("spa_audit_status_rederivation_mismatch")
        if spa_result.get("analysis") != expected_spa_analysis:
            blockers.append("spa_audit_analysis_rederivation_mismatch")
    except Exception as exc:
        blockers.append(f"spa_audit_rederivation:{type(exc).__name__}")

    spa_refs, spa_source_blockers = _verify_spa_sources(panel, repository_root, policy)
    refs.extend(spa_refs)
    blockers.extend(spa_source_blockers)

    candidate = benchmark = receipt = None
    package_refs: list[dict] = []
    package_blockers: list[str] = []
    stager_success = stager_doc.get("status") in policy["required_stager_statuses"]
    if stager_success:
        candidate, benchmark, receipt, package_refs, package_blockers = _package_documents(
            stager_doc, repository_root, policy, contracts["artifact_policy"])
        refs.extend(package_refs)
        blockers.extend(package_blockers)

    candidate_sources = uniqueness.get("source_artifacts", {}).get("candidate", {})
    benchmark_sources = uniqueness.get("source_artifacts", {}).get("benchmark", {})
    candidate_hashes = {
        "stager": stager_doc.get("candidate_result_sha256"),
        "package": candidate.get("result_sha256") if isinstance(candidate, dict) else None,
        "cohort_uniqueness": candidate_sources.get("result_sha256"),
        "inference": inference.get("source_result_sha256"),
    }
    benchmark_hashes = {
        "stager": stager_doc.get("benchmark_result_sha256"),
        "package": benchmark.get("result_sha256") if isinstance(benchmark, dict) else None,
        "cohort_uniqueness": benchmark_sources.get("result_sha256"),
        "spa_panel": panel.get("benchmark", {}).get("source_result_sha256"),
    }
    candidate_values = {value for value in candidate_hashes.values() if value is not None}
    benchmark_values = {value for value in benchmark_hashes.values() if value is not None}
    if len(candidate_values) != 1:
        blockers.append("candidate_result_sha256_binding_mismatch")
    if len(benchmark_values) != 1:
        blockers.append("benchmark_result_sha256_binding_mismatch")
    candidate_sha = next(iter(candidate_values), None)
    benchmark_sha = next(iter(benchmark_values), None)
    candidate_models = [source for source in panel.get("models", [])
                        if source.get("source_result_sha256") == candidate_sha]
    if len(candidate_models) != 1:
        blockers.append("spa_candidate_source_binding_count_invalid")
        candidate_model = {}
    else:
        candidate_model = candidate_models[0]
    result["candidate_portfolio_id"] = candidate_model.get("portfolio_id")
    result["benchmark_portfolio_id"] = panel.get("benchmark", {}).get("portfolio_id")
    result["benchmark_strategy_id"] = benchmark_sources.get("strategy_id")

    experiment, experiment_blockers = _factory_experiment(
        factory, stager_doc.get("bundle_sha256"))
    blockers.extend(experiment_blockers)
    experiment = experiment or {}
    metrics = experiment.get("metrics") if isinstance(experiment.get("metrics"), dict) else {}
    factory_gates = experiment.get("gates") if isinstance(
        experiment.get("gates"), list) else []
    factory_gate_codes = sorted(row.get("code") for row in factory_gates
                                if isinstance(row, dict) and isinstance(row.get("code"), str))
    all_factory_gates_passed = (
        factory_gate_codes == policy["required_factory_gate_codes"]
        and all(row.get("passed") is True for row in factory_gates)
        and experiment.get("all_gates_passed") is True
        and experiment.get("gate_count") == len(policy["required_factory_gate_codes"])
        and experiment.get("passed_count") == len(policy["required_factory_gate_codes"])
        and experiment.get("failed_codes") == []
    )
    stress_rows = metrics.get("cost_stress") if isinstance(metrics.get("cost_stress"), list) else []
    qualifying_stress = [row for row in stress_rows if isinstance(row, dict)
                         and _number(row.get("cost_multiplier"))
                         and float(row["cost_multiplier"]) >= policy[
                             "minimum_cost_stress_multiplier"]]
    worst_stressed_expectancy = min(
        (float(row["expectancy"]) for row in qualifying_stress
         if _number(row.get("expectancy"))), default=None)

    hac = inference.get("hac") if isinstance(inference.get("hac"), dict) else {}
    bootstrap = (inference.get("moving_block_bootstrap")
                 if isinstance(inference.get("moving_block_bootstrap"), dict) else {})
    sample = inference.get("sample") if isinstance(inference.get("sample"), dict) else {}
    hac_interval = hac.get("confidence_interval") if isinstance(
        hac.get("confidence_interval"), list) else []
    bootstrap_interval = bootstrap.get("confidence_interval") if isinstance(
        bootstrap.get("confidence_interval"), list) else []
    hac_lower = hac_interval[0] if len(hac_interval) == 2 else None
    bootstrap_lower = bootstrap_interval[0] if len(bootstrap_interval) == 2 else None

    analysis = spa_result.get("analysis") if isinstance(
        spa_result.get("analysis"), dict) else {}
    configurations = analysis.get("configurations") if isinstance(
        analysis.get("configurations"), list) else []
    candidate_id = candidate_model.get("portfolio_id")
    candidate_spa_means: list[float] = []
    candidate_best_all = bool(configurations)
    for configuration in configurations:
        if not isinstance(configuration, dict) or configuration.get("available") is not True:
            candidate_best_all = False
            continue
        candidate_best_all = candidate_best_all and (
            configuration.get("best_sample_model_id") == candidate_id)
        rows = [row for row in configuration.get("models", [])
                if isinstance(row, dict) and row.get("model_id") == candidate_id]
        if len(rows) != 1 or not _number(rows[0].get("mean_return_differential")):
            candidate_best_all = False
        else:
            candidate_spa_means.append(float(rows[0]["mean_return_differential"]))
    minimum_candidate_spa_mean = min(candidate_spa_means, default=None)

    checks = [
        _check("stager_terminal", "package", stager_success,
               stager_doc.get("status"), policy["required_stager_statuses"]),
        _check("staged_package_revalidated", "package",
               stager_success and not package_blockers and candidate is not None
               and benchmark is not None and receipt is not None,
               not package_blockers if stager_success else False, True),
        _check("factory_terminal", "factory",
               factory.get("status") == policy["required_factory_status"],
               factory.get("status"), policy["required_factory_status"]),
        _check("factory_current_experiment_terminal", "factory",
               experiment.get("status") == policy["required_factory_status"],
               experiment.get("status"), policy["required_factory_status"]),
        _check("factory_all_preregistered_gates", "factory", all_factory_gates_passed,
               {"codes": factory_gate_codes,
                "passed": sum(row.get("passed") is True for row in factory_gates)},
               {"codes": policy["required_factory_gate_codes"],
                "passed": len(policy["required_factory_gate_codes"])}),
        _check("factory_positive_cost_stress", "factory",
               bool(qualifying_stress) and _strictly_above(worst_stressed_expectancy, 0.0),
               {"qualifying_count": len(qualifying_stress),
                "worst_expectancy": worst_stressed_expectancy},
               {"minimum_multiplier": policy["minimum_cost_stress_multiplier"],
                "worst_expectancy": "> 0"}),
        _check("candidate_hash_binding", "binding", len(candidate_values) == 1,
               candidate_hashes, "all_non_null_hashes_equal"),
        _check("benchmark_hash_binding", "binding", len(benchmark_values) == 1,
               benchmark_hashes, "all_non_null_hashes_equal"),
        _check("uniqueness_incremental_alpha", "uniqueness",
               cohort.get("classification") == policy[
                   "required_uniqueness_classification"],
               cohort.get("classification"), policy["required_uniqueness_classification"]),
        _check("inference_reviewable", "inference",
               inference.get("status") == policy["required_inference_status"],
               inference.get("status"), policy["required_inference_status"]),
        _check("inference_daily_observations", "inference",
               isinstance(sample.get("daily_observations"), int)
               and sample["daily_observations"] >= policy[
                   "minimum_inference_daily_observations"],
               sample.get("daily_observations"), policy[
                   "minimum_inference_daily_observations"]),
        _check("inference_effective_observations", "inference",
               _number(sample.get("effective_observations"))
               and float(sample["effective_observations"]) >= policy[
                   "minimum_effective_observations"],
               sample.get("effective_observations"), policy[
                   "minimum_effective_observations"]),
        _check("inference_effective_fraction", "inference",
               _number(sample.get("effective_fraction"))
               and float(sample["effective_fraction"]) >= policy[
                   "minimum_effective_observation_fraction"],
               sample.get("effective_fraction"), policy[
                   "minimum_effective_observation_fraction"]),
        _check("inference_hac_positive_lower_bound", "inference",
               hac.get("available") is True and _strictly_above(
                   hac_lower, policy["minimum_hac_lower_confidence_bound"]),
               hac_lower, f"> {policy['minimum_hac_lower_confidence_bound']}"),
        _check("inference_bootstrap_positive_lower_bound", "inference",
               bootstrap.get("available") is True and _strictly_above(
                   bootstrap_lower, policy["minimum_bootstrap_lower_confidence_bound"]),
               bootstrap_lower,
               f"> {policy['minimum_bootstrap_lower_confidence_bound']}"),
        _check("spa_reviewable", "spa",
               spa_result.get("status") == policy["required_spa_status"],
               spa_result.get("status"), policy["required_spa_status"]),
        _check("spa_family_complete", "spa",
               spa_result.get("sample", {}).get("family_complete") is True,
               spa_result.get("sample", {}).get("family_complete"), True),
        _check("spa_robust_block_lengths", "spa",
               analysis.get("robust_across_configured_block_lengths") is True,
               analysis.get("robust_across_configured_block_lengths"), True),
        _check("spa_candidate_best_sample", "spa", candidate_best_all,
               {"candidate_portfolio_id": candidate_id,
                "configuration_count": len(configurations)},
               "candidate_is_best_sample_model_in_every_configuration"),
        _check("spa_candidate_positive_mean_differential", "spa",
               len(candidate_spa_means) == len(configurations) > 0
               and _strictly_above(minimum_candidate_spa_mean, policy[
                   "minimum_candidate_spa_mean_return_differential"]),
               minimum_candidate_spa_mean,
               f"> {policy['minimum_candidate_spa_mean_return_differential']}"),
    ]

    result["bindings"] = {
        "bundle_sha256": stager_doc.get("bundle_sha256"),
        "candidate_result_sha256": candidate_sha,
        "benchmark_result_sha256": benchmark_sha,
        "export_receipt_sha256": stager_doc.get("export_receipt_sha256"),
        "factory_artifact_sha256": factory.get("artifact_sha256"),
        "stager_health_sha256": stager_doc.get("health_sha256"),
        "cohort_audit_sha256": cohort.get("audit_sha256"),
        "inference_audit_sha256": inference.get("audit_sha256"),
        "spa_panel_sha256": panel.get("panel_sha256"),
        "spa_audit_sha256": spa_result.get("audit_sha256"),
        "factory_config_sha256": sha256_file(factory_config_path),
        "artifact_policy_sha256": sa.sha256_obj(contracts["artifact_policy"]),
        "inference_policy_sha256": ia.sha256_obj(contracts["inference_policy"]),
        "uniqueness_policy_sha256": su.sha256_obj(contracts["uniqueness_policy"]),
        "spa_policy_sha256": spa_policy_sha,
    }
    result["evidence_snapshot"] = {
        "factory": {
            "status": factory.get("status"),
            "current_experiment_status": experiment.get("status"),
            "gate_count": len(factory_gates),
            "holdout_expectancy": metrics.get("holdout_expectancy"),
            "holdout_profit_factor": metrics.get("holdout_profit_factor"),
            "holdout_max_drawdown": metrics.get("holdout_max_drawdown"),
            "deflated_sharpe_probability": metrics.get("deflated_sharpe_probability"),
            "probability_backtest_overfitting": metrics.get(
                "probability_backtest_overfitting"),
            "worst_stressed_expectancy": worst_stressed_expectancy,
        },
        "uniqueness": {
            "classification": cohort.get("classification"),
            "residual_alpha_lower_bound": uniqueness.get(
                "residual_alpha", {}).get("confidence_interval", [None])[0]
            if isinstance(uniqueness.get("residual_alpha"), dict) else None,
            "incremental_portfolio": uniqueness.get("incremental_portfolio"),
        },
        "inference": {
            "status": inference.get("status"),
            "daily_observations": sample.get("daily_observations"),
            "effective_observations": sample.get("effective_observations"),
            "effective_fraction": sample.get("effective_fraction"),
            "hac_lower_bound": hac_lower,
            "bootstrap_lower_bound": bootstrap_lower,
        },
        "spa": {
            "status": spa_result.get("status"),
            "family_complete": spa_result.get("sample", {}).get("family_complete"),
            "robust_across_configured_block_lengths": analysis.get(
                "robust_across_configured_block_lengths"),
            "candidate_best_sample_all_configurations": candidate_best_all,
            "candidate_minimum_mean_return_differential": minimum_candidate_spa_mean,
        },
    }
    result["evidence_checks"] = checks
    result["input_artifacts"] = sorted(refs, key=lambda row: (row["role"], row["path"]))
    source_times = [
        stager_doc.get("generated_utc"),
        factory.get("generated_utc"),
        spa_result.get("generated_utc"),
        uniqueness.get("as_of_utc"),
    ]
    for value in source_times:
        parsed = _parse_utc(value)
        if parsed is not None and generated < parsed:
            blockers.append("gate_generated_before_source_artifact")
            break
    result["blockers"] = blockers
    finished = _finish(result)
    validation_errors = validate_result(finished)
    if validation_errors:
        fallback = _base(_utc(generated), policy)
        fallback["input_artifacts"] = result["input_artifacts"]
        fallback["blockers"] = [f"result_self_validation:{error}"
                                for error in validation_errors]
        return _finish(fallback), 1
    return finished, 1 if finished["status"] == "blocked" else 0


def validate_result(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["evidence_gate_result_not_object"]
    errors: list[str] = []
    if set(document) != RESULT_KEYS:
        errors.append("evidence_gate_result_keys_invalid")
    for key, expected in SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"evidence_gate_result_safety_invalid:{key}")
    if (document.get("tool") != TOOL or document.get("schema") != RESULT_SCHEMA
            or document.get("ok") is not True):
        errors.append("evidence_gate_result_identity_invalid")
    if _parse_utc(document.get("generated_utc")) is None:
        errors.append("evidence_gate_result_generated_utc_invalid")
    if not isinstance(document.get("policy_version"), str) or not _SEMVER_RE.fullmatch(
            document.get("policy_version", "")):
        errors.append("evidence_gate_result_policy_version_invalid")
    for key in ("policy_sha256", "result_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                document.get(key, "")):
            errors.append(f"evidence_gate_result_hash_invalid:{key}")
    target = document.get("target_session_date")
    run_id = document.get("post_close_run_id")
    if target is not None and not _valid_date(target):
        errors.append("evidence_gate_result_target_invalid")
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_RE.fullmatch(run_id)):
        errors.append("evidence_gate_result_run_id_invalid")
    for key in ("candidate_strategy_id", "candidate_portfolio_id",
                "benchmark_strategy_id", "benchmark_portfolio_id"):
        value = document.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            errors.append(f"evidence_gate_result_id_invalid:{key}")
    status = document.get("status")
    if status not in {"blocked", "not_eligible",
                      "eligible_for_shadow_observation_review"}:
        errors.append("evidence_gate_result_status_invalid")
    refs = document.get("input_artifacts")
    if not isinstance(refs, list):
        errors.append("evidence_gate_result_input_artifacts_invalid")
        refs = []
    roles: list[str] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict) or set(ref) != {
                "role", "path", "file_sha256", "schema", "semantic_sha256"}:
            errors.append(f"evidence_gate_result_input_ref_invalid:{index}")
            continue
        if not isinstance(ref.get("role"), str) or not ref["role"]:
            errors.append(f"evidence_gate_result_input_role_invalid:{index}")
        else:
            roles.append(ref["role"])
        if not isinstance(ref.get("path"), str) or not ref["path"]:
            errors.append(f"evidence_gate_result_input_path_invalid:{index}")
        if not isinstance(ref.get("file_sha256"), str) or not _SHA_RE.fullmatch(
                ref.get("file_sha256", "")):
            errors.append(f"evidence_gate_result_input_file_hash_invalid:{index}")
        semantic = ref.get("semantic_sha256")
        if semantic is not None and (not isinstance(semantic, str)
                                     or not _SHA_RE.fullmatch(semantic)):
            errors.append(f"evidence_gate_result_input_semantic_hash_invalid:{index}")
    if len(roles) != len(set(roles)):
        errors.append("evidence_gate_result_input_roles_duplicate")
    checks = document.get("evidence_checks")
    if not isinstance(checks, list):
        errors.append("evidence_gate_result_checks_invalid")
        checks = []
    codes: list[str] = []
    allowed_categories = {"factory", "package", "binding", "uniqueness", "inference", "spa"}
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or set(check) != {
                "code", "category", "passed", "observed", "required"}:
            errors.append(f"evidence_gate_result_check_invalid:{index}")
            continue
        if not isinstance(check.get("code"), str) or not check["code"]:
            errors.append(f"evidence_gate_result_check_code_invalid:{index}")
        else:
            codes.append(check["code"])
        if check.get("category") not in allowed_categories:
            errors.append(f"evidence_gate_result_check_category_invalid:{index}")
        if check.get("passed") not in (True, False):
            errors.append(f"evidence_gate_result_check_passed_invalid:{index}")
    if len(codes) != len(set(codes)):
        errors.append("evidence_gate_result_check_codes_duplicate")
    expected_failed = [row["code"] for row in checks
                       if isinstance(row, dict) and row.get("passed") is not True
                       and isinstance(row.get("code"), str)]
    if document.get("failed_check_codes") != expected_failed:
        errors.append("evidence_gate_result_failed_codes_mismatch")
    if document.get("passed_check_count") != sum(
            isinstance(row, dict) and row.get("passed") is True for row in checks):
        errors.append("evidence_gate_result_passed_count_mismatch")
    blockers = document.get("blockers")
    if (not isinstance(blockers, list)
            or any(not isinstance(value, str) or not value for value in blockers)
            or blockers != sorted(set(blockers))):
        errors.append("evidence_gate_result_blockers_invalid")
        blockers = []
    expected_status = ("blocked" if blockers else "not_eligible" if expected_failed
                       else "eligible_for_shadow_observation_review")
    if status != expected_status:
        errors.append("evidence_gate_result_status_derivation_mismatch")
    if document.get("eligible_for_shadow_observation_review") is not (
            status == "eligible_for_shadow_observation_review"):
        errors.append("evidence_gate_result_eligibility_flag_mismatch")
    if status == "eligible_for_shadow_observation_review":
        required_roles = {
            "stager", "factory", "cohort_audit", "inference_audit", "spa_panel",
            "spa_audit", "candidate_portfolio", "benchmark_portfolio", "export_receipt",
        }
        if not required_roles.issubset(set(roles)):
            errors.append("evidence_gate_result_eligible_inputs_missing")
        if any(document.get(key) is None for key in (
                "target_session_date", "post_close_run_id", "candidate_strategy_id",
                "candidate_portfolio_id", "benchmark_strategy_id",
                "benchmark_portfolio_id")):
            errors.append("evidence_gate_result_eligible_identity_missing")
    if document.get("limitations") != LIMITATIONS:
        errors.append("evidence_gate_result_limitations_invalid")
    if (document.get("shadow_observation_started") is not False
            or document.get("human_approval_required") is not True
            or document.get("production_change_required") is not True
            or document.get("profitability_status") != "not_established"
            or document.get("actual_orders_count") != 0):
        errors.append("evidence_gate_result_authority_invalid")
    if not isinstance(document.get("bindings"), dict) or not isinstance(
            document.get("evidence_snapshot"), dict):
        errors.append("evidence_gate_result_sections_invalid")
    if document.get("result_sha256") != sha256_obj(_payload(document, "result_sha256")):
        errors.append("evidence_gate_result_sha256_mismatch")
    return sorted(set(errors))


def _atomic_write_once(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_strategy_gate_",
                                     suffix=path.suffix)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hash-bound strategy evidence gate for human shadow review only.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--stager", required=True)
    evaluate.add_argument("--factory", required=True)
    evaluate.add_argument("--cohort-audit", required=True)
    evaluate.add_argument("--inference-audit", required=True)
    evaluate.add_argument("--spa-panel", required=True)
    evaluate.add_argument("--spa-audit", required=True)
    evaluate.add_argument("--repository-root", default=str(ROOT))
    evaluate.add_argument("--policy", default=str(POLICY_DEFAULT))
    evaluate.add_argument("--factory-config", default=str(FACTORY_CONFIG_DEFAULT))
    evaluate.add_argument("--artifact-policy", default=str(ARTIFACT_POLICY_DEFAULT))
    evaluate.add_argument("--inference-policy", default=str(INFERENCE_POLICY_DEFAULT))
    evaluate.add_argument("--uniqueness-policy", default=str(UNIQUENESS_POLICY_DEFAULT))
    evaluate.add_argument("--spa-policy", default=str(SPA_POLICY_DEFAULT))
    evaluate.add_argument("--generated-utc")
    evaluate.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        output = policy_summary(Path(args.policy))
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["ok"] else 1
    if args.command == "validate":
        try:
            document = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
            errors = validate_result(document)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors = [f"artifact_unreadable:{type(exc).__name__}"]
        print(json.dumps({"ok": not errors, "errors": errors}, indent=2, sort_keys=True))
        return 0 if not errors else 1
    output, rc = run_gate(
        stager_path=Path(args.stager),
        factory_path=Path(args.factory),
        cohort_audit_path=Path(args.cohort_audit),
        inference_audit_path=Path(args.inference_audit),
        spa_panel_path=Path(args.spa_panel),
        spa_audit_path=Path(args.spa_audit),
        repository_root=Path(args.repository_root),
        policy_path=Path(args.policy),
        factory_config_path=Path(args.factory_config),
        artifact_policy_path=Path(args.artifact_policy),
        inference_policy_path=Path(args.inference_policy),
        uniqueness_policy_path=Path(args.uniqueness_policy),
        spa_policy_path=Path(args.spa_policy),
        generated_utc=args.generated_utc,
    )
    try:
        _atomic_write_once(Path(args.out), output)
    except FileExistsError as exc:
        print(json.dumps(_refuse(str(exc)), indent=2, sort_keys=True))
        return 1
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
