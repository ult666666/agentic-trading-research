#!/usr/bin/env python3
"""Current-run-bound, write-once staging for factory-approved research artifacts.

The controller runs only after the grid worker and independent research factory. It stages the
current bundle only when both health artifacts are valid, agree on the exact bundle hash, and the
factory gave that bundle its strongest category-D review-candidate terminal. Staging is local
research preservation, not registry registration, strategy activation, or order execution.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import artifact_provenance as provenance  # noqa: E402
from quant import grid_worker as gw  # noqa: E402
from quant import research_factory as rf  # noqa: E402
from quant import strategy_artifacts as sa  # noqa: E402

TOOL = "quant.strategy_artifact_stager"
SCHEMA = "quant_strategy_artifact_stager/1.0"
STATUSES = {"not_candidate", "staged", "verified_existing", "blocked"}
EXPERIMENT_ROOT_DEFAULT = ROOT / "data" / "runtime" / "experiments"
OUTPUT_ROOT_DEFAULT = sa.EXPORT_ROOT_DEFAULT
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

HEALTH_SAFETY = {
    **sa.OUTPUT_SAFETY,
    "registry_mutated": False,
    "performance_evaluated": False,
    "profitability_status": "not_established",
}
_HEALTH_KEYS = {
    *HEALTH_SAFETY,
    "tool", "ok", "schema", "target_session_date", "post_close_run_id",
    "generated_utc", "status", "staging_attempted", "grid_health_file_sha256",
    "factory_health_file_sha256", "grid_status", "factory_status", "bundle_path",
    "bundle_sha256", "package_path", "export_receipt_sha256",
    "candidate_result_sha256", "benchmark_result_sha256", "reasons", "blockers",
    "health_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "health_sha256"}


def _health(target: str, run_id: str) -> dict:
    return {
        **HEALTH_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": SCHEMA,
        "target_session_date": target,
        "post_close_run_id": run_id,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "blocked",
        "staging_attempted": False,
        "grid_health_file_sha256": None,
        "factory_health_file_sha256": None,
        "grid_status": None,
        "factory_status": None,
        "bundle_path": None,
        "bundle_sha256": None,
        "package_path": None,
        "export_receipt_sha256": None,
        "candidate_result_sha256": None,
        "benchmark_result_sha256": None,
        "reasons": [],
        "blockers": [],
    }


def _finish(document: dict) -> dict:
    document["health_sha256"] = _sha(_payload(document))
    return document


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def validate_health(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["stager_health_not_object"]
    errors = []
    if set(document) != _HEALTH_KEYS:
        errors.append("stager_health_top_level_keys_invalid")
    for key, expected in HEALTH_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"stager_health_safety_invalid:{key}")
    if document.get("tool") != TOOL or document.get("ok") is not True or document.get(
            "schema") != SCHEMA:
        errors.append("stager_health_identity_invalid")
    target = document.get("target_session_date")
    if not isinstance(target, str) or not provenance.valid_postclose_run_id(
            document.get("post_close_run_id"), target):
        errors.append("stager_health_target_or_run_id_invalid")
    if provenance.parse_utc_strict(document.get("generated_utc")) is None:
        errors.append("stager_health_generated_utc_invalid")
    status = document.get("status")
    if status not in STATUSES:
        errors.append("stager_health_status_invalid")
    if document.get("staging_attempted") not in (True, False):
        errors.append("stager_health_staging_attempted_invalid")
    for key in ("grid_health_file_sha256", "factory_health_file_sha256", "bundle_sha256",
                "export_receipt_sha256", "candidate_result_sha256",
                "benchmark_result_sha256"):
        value = document.get(key)
        if value is not None and not _valid_sha(value):
            errors.append(f"stager_health_hash_invalid:{key}")
    for key in ("grid_status", "factory_status", "bundle_path", "package_path"):
        value = document.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            errors.append(f"stager_health_field_invalid:{key}")
    for key in ("reasons", "blockers"):
        values = document.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value
                                               for value in values):
            errors.append(f"stager_health_list_invalid:{key}")
    if status == "not_candidate":
        if document.get("staging_attempted") is not False or not document.get("reasons"):
            errors.append("stager_health_not_candidate_contract_invalid")
        if document.get("blockers") != []:
            errors.append("stager_health_not_candidate_has_blockers")
        for key in ("package_path", "export_receipt_sha256", "candidate_result_sha256",
                    "benchmark_result_sha256"):
            if document.get(key) is not None:
                errors.append(f"stager_health_not_candidate_has_artifact:{key}")
    if status == "blocked" and not document.get("blockers"):
        errors.append("stager_health_blocked_without_blocker")
    if status in {"staged", "verified_existing"}:
        if document.get("staging_attempted") is not True:
            errors.append("stager_health_success_without_attempt")
        if document.get("factory_status") != "candidate_for_category_D_governance_review":
            errors.append("stager_health_success_without_factory_candidate")
        for key in ("grid_health_file_sha256", "factory_health_file_sha256",
                    "bundle_sha256", "export_receipt_sha256", "candidate_result_sha256",
                    "benchmark_result_sha256"):
            if not _valid_sha(document.get(key)):
                errors.append(f"stager_health_success_missing:{key}")
        for key in ("bundle_path", "package_path"):
            if not isinstance(document.get(key), str) or not document[key]:
                errors.append(f"stager_health_success_missing:{key}")
        if document.get("reasons") != [] or document.get("blockers") != []:
            errors.append("stager_health_success_has_reasons_or_blockers")
    if document.get("health_sha256") != _sha(_payload(document)):
        errors.append("stager_health_sha256_mismatch")
    return sorted(set(errors))


def _read_document(path: Path) -> tuple[dict | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "not_regular_file"
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable_or_malformed:{type(exc).__name__}"
    return (document, None) if isinstance(document, dict) else (None, "not_object")


def _block(health: dict, *reasons: str) -> tuple[dict, int]:
    health["status"] = "blocked"
    health["blockers"] = sorted(set(reasons))
    return _finish(health), 1


def _not_candidate(health: dict, *reasons: str) -> tuple[dict, int]:
    health["status"] = "not_candidate"
    health["reasons"] = sorted(set(reasons))
    return _finish(health), 0


def run_stager(*, target: str, run_id: str, grid_health_path: Path,
               factory_health_path: Path, experiment_root: Path,
               output_root: Path, policy_path: Path = sa.POLICY_DEFAULT
               ) -> tuple[dict, int]:
    health = _health(target, run_id)
    if not provenance.valid_postclose_run_id(run_id, target):
        return _block(health, "run_id_invalid_for_target")
    grid, problem = _read_document(grid_health_path)
    if problem:
        return _block(health, f"grid_health:{problem}")
    factory, problem = _read_document(factory_health_path)
    if problem:
        return _block(health, f"factory_health:{problem}")
    health["grid_health_file_sha256"] = _sha_file(grid_health_path)
    health["factory_health_file_sha256"] = _sha_file(factory_health_path)
    health["grid_status"] = grid.get("status")
    health["factory_status"] = factory.get("status")
    input_errors = [f"grid_health:{value}" for value in gw.validate_health(grid)]
    input_errors.extend(f"factory_health:{value}" for value in rf.validate_artifact(factory))
    for name, document in (("grid", grid), ("factory", factory)):
        if document.get("target_session_date") != target:
            input_errors.append(f"{name}_health_target_mismatch")
        if document.get("post_close_run_id") != run_id:
            input_errors.append(f"{name}_health_run_id_mismatch")
    policy, policy_errors = sa.load_policy(policy_path)
    input_errors.extend(f"artifact_policy:{value}" for value in policy_errors)
    if policy is None:
        input_errors.append("artifact_policy_missing")
    if input_errors:
        return _block(health, *input_errors)

    grid_has_bundle = grid["status"] in {"bundle_created", "bundle_existing"}
    if not grid_has_bundle:
        return _not_candidate(
            health, f"factory_status:{factory['status']}", f"grid_status:{grid['status']}")
    if factory["status"] != "candidate_for_category_D_governance_review":
        return _not_candidate(
            health, f"factory_status:{factory['status']}", f"grid_status:{grid['status']}")

    try:
        root = experiment_root.resolve(strict=True)
        grid_directory = Path(grid["bundle_path"])
        resolved_directory = grid_directory.resolve(strict=True)
        relative_directory = resolved_directory.relative_to(root)
    except (OSError, TypeError, ValueError):
        return _block(health, "grid_bundle_path_escape_or_missing")
    if (grid_directory.is_symlink() or not resolved_directory.is_dir()
            or len(relative_directory.parts) != 1
            or not _ID_RE.fullmatch(relative_directory.name)):
        return _block(health, "grid_bundle_directory_invalid")
    bundle_path = resolved_directory / "bundle.json"
    if bundle_path.is_symlink() or not bundle_path.is_file():
        return _block(health, "grid_bundle_file_missing_or_invalid")
    health["bundle_path"] = str(bundle_path)
    health["bundle_sha256"] = grid["bundle_sha256"]
    relative_bundle = bundle_path.relative_to(root).as_posix()
    matches = [
        experiment for experiment in factory["experiments"]
        if experiment.get("bundle_path") == relative_bundle
    ]
    if len(matches) != 1:
        return _block(health, "current_grid_bundle_not_unique_in_factory_health")
    match = matches[0]
    if match.get("status") != "candidate_for_category_D_governance_review":
        return _not_candidate(
            health, f"current_bundle_factory_status:{match.get('status', 'missing')}",
            f"factory_status:{factory['status']}")
    if factory["status"] != "candidate_for_category_D_governance_review":
        return _block(health, "current_bundle_candidate_but_factory_terminal_mismatch")
    metrics = match.get("metrics") if isinstance(match.get("metrics"), dict) else {}
    if (match.get("all_gates_passed") is not True
            or match.get("activation_authority") is not False
            or match.get("profitability_established") is not False
            or metrics.get("bundle_sha256") != grid["bundle_sha256"]):
        return _block(health, "factory_candidate_bundle_contract_mismatch")
    bundle, problem = _read_document(bundle_path)
    if problem:
        return _block(health, f"bundle:{problem}")
    if bundle.get("bundle_sha256") != grid["bundle_sha256"]:
        return _block(health, "grid_bundle_sha256_mismatch")
    preflight = sa.inspect_factory_bundle(bundle_path, experiment_root, policy)
    if preflight.get("status") != "ready":
        details = preflight.get("blockers") or preflight.get("errors") or [
            preflight.get("error", "unknown")]
        return _block(health, *[f"strategy_export_preflight:{value}" for value in details])

    health["staging_attempted"] = True
    destination = output_root / bundle["bundle_id"]
    if destination.exists():
        result = sa.verify_export_package(bundle_path, experiment_root, output_root, policy)
        success_status = "verified_existing"
    else:
        result = sa.export_factory_bundle(bundle_path, experiment_root, output_root, policy)
        if result.get("error") == "strategy_artifact_package_already_exists":
            result = sa.verify_export_package(bundle_path, experiment_root, output_root, policy)
            success_status = "verified_existing"
        elif result.get("ok"):
            result = sa.verify_export_package(bundle_path, experiment_root, output_root, policy)
            success_status = "staged"
        else:
            success_status = "staged"
    if not result.get("ok") or result.get("status") != "verified_existing":
        details = result.get("errors") or result.get("blockers") or [
            result.get("error", "unknown")]
        return _block(health, *[f"strategy_artifact_stage:{value}" for value in details])
    receipt = result["receipt"]
    exported = receipt["exported_artifacts"]
    health.update({
        "status": success_status,
        "package_path": result["package_path"],
        "export_receipt_sha256": receipt["receipt_sha256"],
        "candidate_result_sha256": exported["candidate_portfolio"]["result_sha256"],
        "benchmark_result_sha256": exported["benchmark_reference"]["result_sha256"],
    })
    return _finish(health), 0


def _atomic_write(path: Path, document: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_strategy_stager_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage only current-run factory-approved offline strategy artifacts.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--target", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--grid-health", required=True)
    run.add_argument("--factory-health", required=True)
    run.add_argument("--experiment-root", default=str(EXPERIMENT_ROOT_DEFAULT))
    run.add_argument("--output-root", default=str(OUTPUT_ROOT_DEFAULT))
    run.add_argument("--policy", default=str(sa.POLICY_DEFAULT))
    run.add_argument("--out", required=True)
    run.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        document, problem = _read_document(Path(args.path))
        errors = [problem] if problem else validate_health(document)
        print(json.dumps({"ok": not errors, "errors": errors}, indent=2, sort_keys=True))
        return 0 if not errors else 1
    health, rc = run_stager(
        target=args.target, run_id=args.run_id,
        grid_health_path=Path(args.grid_health),
        factory_health_path=Path(args.factory_health),
        experiment_root=Path(args.experiment_root),
        output_root=Path(args.output_root), policy_path=Path(args.policy))
    write_error = _atomic_write(Path(args.out), health, args.replace)
    if write_error:
        health = {**health, "ok": False, "error": write_error}
        rc = 1
    print(json.dumps(health, indent=2, sort_keys=True, allow_nan=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
