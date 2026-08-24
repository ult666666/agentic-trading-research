#!/usr/bin/env python3
"""Dependence-aware uncertainty and purge/embargo diagnostics for the offline quant lab.

Implements a Bartlett-kernel Newey-West standard error for a sample mean, deterministic moving
overlapping-block bootstrap intervals, positive-autocorrelation effective sample size, and
group-aware label-interval purge/embargo auditing. It changes no factory or promotion gate and
deliberately does not claim to implement Hansen's SPA test.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import features as qf  # noqa: E402
from quant import portfolio_backtest as pb  # noqa: E402

TOOL = "quant.inference_audit"
POLICY_SCHEMA = "quant_inference_policy/1.0"
INFERENCE_SCHEMA = "quant_inference_audit/1.0"
ASSIGNMENTS_SCHEMA = "quant_interval_assignments/1.0"
PURGE_SCHEMA = "quant_purge_embargo_audit/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_inference_policy.json"
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NUM = (int, float)
_MAX_BYTES = 128 * 1024 * 1024

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "strategy_change_allowed": False,
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "spa_claim_allowed": False,
    "broker_access": "none",
}
OUTPUT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "spa_claim_allowed": False,
}
_POLICY_KEYS = {
    "schema", "version", "confidence_level", "normal_critical_value",
    "minimum_hac_observations", "minimum_bootstrap_observations", "hac_kernel",
    "hac_lag_rule", "bootstrap_method", "bootstrap_replications",
    "bootstrap_block_length_rule", "bootstrap_quantile_rule", "bootstrap_seed",
    "effective_sample_size_rule", "interval_purge_rule", "group_purge_rule",
    "embargo_rule", "embargo_calendar_days", "spa_status", "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "confidence_level": 0.95,
    "normal_critical_value": 1.9599639845,
    "minimum_hac_observations": 30,
    "minimum_bootstrap_observations": 60,
    "hac_kernel": "bartlett",
    "hac_lag_rule": "floor_4_n_over_100_power_2_over_9_capped_n_minus_1",
    "bootstrap_method": "moving_overlapping_blocks",
    "bootstrap_replications": 2000,
    "bootstrap_block_length_rule": "ceil_cube_root_n_capped_n",
    "bootstrap_quantile_rule": "linear_interpolation_type_7",
    "bootstrap_seed": 1729,
    "effective_sample_size_rule": "positive_autocorrelation_sum_through_hac_lag",
    "interval_purge_rule": (
        "remove_train_label_intervals_overlapping_any_test_label_interval"),
    "group_purge_rule": "remove_train_observations_sharing_any_test_group",
    "embargo_rule": (
        "remove_train_event_dates_after_test_label_end_through_embargo_days"),
    "embargo_calendar_days": 10,
    "spa_status": "not_implemented_requires_separate_reference_validation",
}
_ASSIGNMENT_KEYS = {"schema", "fold_id", "source_artifact_sha256", "observations"}
_OBSERVATION_KEYS = {
    "observation_id", "group_id", "event_date", "label_start_date", "label_end_date", "split",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _payload(value: dict, hash_key: str) -> dict:
    return {key: item for key, item in value.items() if key != hash_key}


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    output = round(float(value), 10)
    return 0.0 if output == 0 else output


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _refuse(error: str, **extra) -> dict:
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["inference_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("inference_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"inference_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            document.get("version", "")):
        errors.append("inference_policy_version_invalid")
    for key in ("minimum_hac_observations", "minimum_bootstrap_observations",
                "bootstrap_replications"):
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            errors.append(f"inference_policy_integer_invalid:{key}")
    if isinstance(document.get("bootstrap_replications"), int) and document[
            "bootstrap_replications"] < 1000:
        errors.append("inference_policy_bootstrap_replications_too_small")
    if (isinstance(document.get("minimum_hac_observations"), int)
            and isinstance(document.get("minimum_bootstrap_observations"), int)
            and document["minimum_bootstrap_observations"] < document[
                "minimum_hac_observations"]):
        errors.append("inference_policy_bootstrap_minimum_below_hac")
    seed = document.get("bootstrap_seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        errors.append("inference_policy_seed_invalid")
    embargo = document.get("embargo_calendar_days")
    if not isinstance(embargo, int) or isinstance(embargo, bool) or embargo < 0:
        errors.append("inference_policy_embargo_invalid")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("inference_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read(path)
    except Exception:
        return None, ["inference_policy_unreadable"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    return {
        **OUTPUT_SAFETY, "tool": TOOL, "ok": policy is not None and not errors,
        "valid": policy is not None and not errors, "errors": errors,
        "version": policy.get("version") if isinstance(policy, dict) else None,
        "policy_sha256": sha256_obj(policy) if isinstance(policy, dict) else None,
        "spa_available": False,
    }


def hac_lag(n: int) -> int:
    if n <= 1:
        return 0
    return min(n - 1, max(0, math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))))


def block_length(n: int) -> int:
    return min(n, max(1, math.ceil(n ** (1.0 / 3.0))))


def _autocovariance(values: list[float], lag: int, mean: float) -> float:
    n = len(values)
    return sum((values[index] - mean) * (values[index - lag] - mean)
               for index in range(lag, n)) / n


def autocorrelations(values: list[float], maximum_lag: int) -> list[float | None]:
    mean = statistics.fmean(values)
    variance = _autocovariance(values, 0, mean)
    if variance <= 0:
        return [None for _ in range(maximum_lag)]
    return [_autocovariance(values, lag, mean) / variance
            for lag in range(1, maximum_lag + 1)]


def effective_sample_size(values: list[float], maximum_lag: int) -> float | None:
    correlations = autocorrelations(values, maximum_lag)
    if not correlations or all(value is None for value in correlations):
        return None
    denominator = 1.0 + 2.0 * sum(max(0.0, float(value)) for value in correlations
                                  if value is not None)
    return min(float(len(values)), max(1.0, len(values) / denominator))


def newey_west_mean(values: list[float], policy: dict) -> dict:
    n = len(values)
    lag = hac_lag(n)
    if n < policy["minimum_hac_observations"]:
        return {"available": False, "reason": "sample_below_minimum_hac_observations",
                "lag": lag, "standard_error": None, "t_statistic": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    mean = statistics.fmean(values)
    gamma_zero = _autocovariance(values, 0, mean)
    if gamma_zero <= 0:
        return {"available": False, "reason": "zero_or_negative_long_run_variance",
                "lag": lag, "standard_error": None, "t_statistic": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    long_run_variance = gamma_zero
    for current_lag in range(1, lag + 1):
        weight = 1.0 - current_lag / (lag + 1.0)
        long_run_variance += 2.0 * weight * _autocovariance(
            values, current_lag, mean)
    if long_run_variance <= 0:
        return {"available": False, "reason": "zero_or_negative_long_run_variance",
                "lag": lag, "standard_error": None, "t_statistic": None,
                "confidence_interval": None, "lower_bound_above_zero": None}
    standard_error = math.sqrt(long_run_variance / n)
    critical = float(policy["normal_critical_value"])
    interval = [mean - critical * standard_error, mean + critical * standard_error]
    return {
        "available": True,
        "reason": None,
        "kernel": "bartlett",
        "lag": lag,
        "long_run_variance": _round(long_run_variance),
        "standard_error": _round(standard_error),
        "t_statistic": _round(mean / standard_error),
        "confidence_interval": [_round(interval[0]), _round(interval[1])],
        "lower_bound_above_zero": interval[0] > 0,
    }


def _quantile_type7(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("quantile_empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def moving_block_bootstrap(values: list[float], policy: dict) -> dict:
    n = len(values)
    length = block_length(n)
    replications = int(policy["bootstrap_replications"])
    if n < policy["minimum_bootstrap_observations"]:
        return {"available": False, "reason": "sample_below_minimum_bootstrap_observations",
                "method": "moving_overlapping_blocks", "block_length": length,
                "replications": replications, "seed": policy["bootstrap_seed"],
                "standard_error": None, "confidence_interval": None,
                "lower_bound_above_zero": None}
    if max(values) == min(values):
        return {"available": False, "reason": "constant_return_series",
                "method": "moving_overlapping_blocks", "block_length": length,
                "replications": replications, "seed": policy["bootstrap_seed"],
                "standard_error": None, "confidence_interval": None,
                "lower_bound_above_zero": None}
    blocks = [values[index:index + length] for index in range(n - length + 1)]
    draws_per_replication = math.ceil(n / length)
    generator = random.Random(policy["bootstrap_seed"])
    means: list[float] = []
    for _ in range(replications):
        sample: list[float] = []
        for _ in range(draws_per_replication):
            sample.extend(blocks[generator.randrange(len(blocks))])
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    alpha = 1.0 - float(policy["confidence_level"])
    lower = _quantile_type7(means, alpha / 2.0)
    upper = _quantile_type7(means, 1.0 - alpha / 2.0)
    return {
        "available": True,
        "reason": None,
        "method": "moving_overlapping_blocks",
        "block_length": length,
        "replications": replications,
        "seed": policy["bootstrap_seed"],
        "standard_error": _round(statistics.stdev(means)),
        "confidence_interval": [_round(lower), _round(upper)],
        "lower_bound_above_zero": lower > 0,
    }


def audit_returns(values: list[float], source_result_sha256: str, policy: dict,
                  dates: list[str] | None = None) -> dict:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return _refuse("inference_policy_invalid", errors=policy_errors)
    if not isinstance(source_result_sha256, str) or not _SHA_RE.fullmatch(
            source_result_sha256):
        return _refuse("inference_source_hash_invalid")
    if not isinstance(values, list) or len(values) < 2 or any(
            not _number(value) or float(value) <= -1.0 for value in values):
        return _refuse("inference_returns_invalid")
    values = [float(value) for value in values]
    if dates is not None and (not isinstance(dates, list) or len(dates) != len(values)
                              or any(qf._parse_date(value) is None for value in dates)
                              or dates != sorted(set(dates))):
        return _refuse("inference_dates_invalid")
    n = len(values)
    lag = hac_lag(n)
    correlations = autocorrelations(values, lag)
    effective_n = effective_sample_size(values, lag)
    hac = newey_west_mean(values, policy)
    bootstrap = moving_block_bootstrap(values, policy)
    sample_stdev = statistics.stdev(values)
    mean = statistics.fmean(values)
    compound = math.prod(1.0 + value for value in values) - 1.0
    if hac["available"] and bootstrap["available"]:
        status = "reviewable"
    elif (n < policy["minimum_hac_observations"]
          or n < policy["minimum_bootstrap_observations"]):
        status = "insufficient_sample"
    else:
        status = "degenerate"
    document = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": INFERENCE_SCHEMA,
        "source_result_sha256": source_result_sha256,
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "status": status,
        "sample": {
            "daily_observations": n,
            "start_date": dates[0] if dates else None,
            "end_date": dates[-1] if dates else None,
            "hac_lag": lag,
            "moving_block_length": block_length(n),
            "effective_observations": _round(effective_n),
            "effective_fraction": _round(effective_n / n) if effective_n is not None else None,
        },
        "descriptive": {
            "arithmetic_mean_daily_return": _round(mean),
            "compound_return": _round(compound),
            "sample_stdev_daily_return": _round(sample_stdev),
            "naive_iid_standard_error": _round(sample_stdev / math.sqrt(n)),
            "autocorrelations": [
                {"lag": index + 1, "value": _round(value)}
                for index, value in enumerate(correlations)
            ],
        },
        "hac": hac,
        "moving_block_bootstrap": bootstrap,
        "multiple_model_inference": {
            "spa_available": False,
            "spa_result": None,
            "reason": policy["spa_status"],
        },
        "interpretation": (
            "Dependence-aware offline uncertainty diagnostic. A positive bound is not live "
            "profitability evidence and cannot alter a strategy or promotion gate."),
        "audit_sha256": "",
    }
    document["audit_sha256"] = sha256_obj(_payload(document, "audit_sha256"))
    errors = validate_inference(document)
    if errors:
        return _refuse("inference_audit_self_validation_failed", errors=errors)
    return document


def audit_portfolio(document: dict, policy: dict) -> dict:
    errors = pb.validate_result(document)
    if errors:
        return _refuse("portfolio_result_invalid", errors=errors)
    values = [float(row["daily_return"]) for row in document["daily_path"]]
    dates = [row["date"] for row in document["daily_path"]]
    return audit_returns(values, document["result_sha256"], policy, dates)


def validate_inference(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["inference_audit_not_object"]
    errors: list[str] = []
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"inference_audit_safety_invalid:{key}")
    if document.get("ok") is not True or document.get("schema") != INFERENCE_SCHEMA:
        errors.append("inference_audit_identity_invalid")
    if document.get("status") not in {"reviewable", "insufficient_sample", "degenerate"}:
        errors.append("inference_audit_status_invalid")
    for key in ("source_result_sha256", "policy_sha256", "audit_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                document.get(key, "")):
            errors.append(f"inference_audit_hash_invalid:{key}")
    multiple = document.get("multiple_model_inference")
    if (not isinstance(multiple, dict) or multiple.get("spa_available") is not False
            or multiple.get("spa_result") is not None):
        errors.append("inference_audit_claims_spa")
    sample = document.get("sample")
    descriptive = document.get("descriptive")
    hac = document.get("hac")
    bootstrap = document.get("moving_block_bootstrap")
    if (not isinstance(sample, dict)
            or not isinstance(sample.get("daily_observations"), int)
            or isinstance(sample.get("daily_observations"), bool)
            or sample.get("daily_observations", 0) < 2
            or not isinstance(sample.get("hac_lag"), int)
            or not isinstance(sample.get("moving_block_length"), int)):
        errors.append("inference_audit_sample_invalid")
    elif (sample["hac_lag"] != hac_lag(sample["daily_observations"])
          or sample["moving_block_length"] != block_length(sample["daily_observations"])):
        errors.append("inference_audit_sample_rules_mismatch")
    if (not isinstance(descriptive, dict)
            or not _number(descriptive.get("arithmetic_mean_daily_return"))
            or not _number(descriptive.get("sample_stdev_daily_return"))
            or not isinstance(descriptive.get("autocorrelations"), list)):
        errors.append("inference_audit_descriptive_invalid")
    for name, section in (("hac", hac), ("bootstrap", bootstrap)):
        if not isinstance(section, dict) or not isinstance(section.get("available"), bool):
            errors.append(f"inference_audit_{name}_invalid")
            continue
        if section["available"]:
            interval = section.get("confidence_interval")
            if (not _number(section.get("standard_error"))
                    or not isinstance(interval, list) or len(interval) != 2
                    or any(not _number(value) for value in interval)
                    or interval[0] > interval[1]
                    or section.get("lower_bound_above_zero") is not (interval[0] > 0)):
                errors.append(f"inference_audit_{name}_available_fields_invalid")
        elif (section.get("standard_error") is not None
              or section.get("confidence_interval") is not None
              or section.get("lower_bound_above_zero") is not None):
            errors.append(f"inference_audit_{name}_unavailable_fields_invalid")
    if isinstance(hac, dict) and isinstance(bootstrap, dict):
        expected_status = ("reviewable" if hac.get("available") is True
                           and bootstrap.get("available") is True
                           else "degenerate" if isinstance(sample, dict)
                           and isinstance(sample.get("daily_observations"), int)
                           and sample["daily_observations"] >= 60
                           else "insufficient_sample")
        if document.get("status") != expected_status:
            errors.append("inference_audit_status_availability_mismatch")
    if document.get("audit_sha256") != sha256_obj(_payload(document, "audit_sha256")):
        errors.append("inference_audit_sha256_mismatch")
    return sorted(set(errors))


def validate_assignments(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["interval_assignments_not_object"]
    errors: list[str] = []
    if set(document) != _ASSIGNMENT_KEYS:
        errors.append("interval_assignments_keys_invalid")
    if document.get("schema") != ASSIGNMENTS_SCHEMA:
        errors.append("interval_assignments_schema_invalid")
    if not isinstance(document.get("fold_id"), str) or not _ID_RE.fullmatch(
            document.get("fold_id", "")):
        errors.append("interval_assignments_fold_id_invalid")
    digest = document.get("source_artifact_sha256")
    if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
        errors.append("interval_assignments_source_hash_invalid")
    rows = document.get("observations")
    if not isinstance(rows, list) or len(rows) < 2:
        return sorted(set(errors + ["interval_assignments_observations_missing"]))
    seen: set[str] = set()
    split_counts = {"train": 0, "test": 0}
    for index, row in enumerate(rows):
        prefix = f"observation[{index}]"
        if not isinstance(row, dict) or set(row) != _OBSERVATION_KEYS:
            errors.append(f"{prefix}.shape_invalid")
            continue
        for key in ("observation_id", "group_id"):
            if not isinstance(row.get(key), str) or not _ID_RE.fullmatch(row.get(key, "")):
                errors.append(f"{prefix}.{key}_invalid")
        if row.get("observation_id") in seen:
            errors.append(f"{prefix}.observation_id_duplicate")
        seen.add(row.get("observation_id"))
        dates = [qf._parse_date(row.get(key)) for key in (
            "label_start_date", "event_date", "label_end_date")]
        if any(value is None for value in dates) or not dates[0] <= dates[1] <= dates[2]:
            errors.append(f"{prefix}.dates_invalid")
        if row.get("split") not in split_counts:
            errors.append(f"{prefix}.split_invalid")
        else:
            split_counts[row["split"]] += 1
    if not split_counts["train"] or not split_counts["test"]:
        errors.append("interval_assignments_require_train_and_test")
    return sorted(set(errors))


def _overlap(a: dict, b: dict) -> bool:
    return not (a["label_end_date"] < b["label_start_date"]
                or b["label_end_date"] < a["label_start_date"])


def purge_and_embargo(assignments: dict, policy: dict) -> dict:
    policy_errors = validate_policy(policy)
    assignment_errors = validate_assignments(assignments)
    if policy_errors or assignment_errors:
        return _refuse("purge_inputs_invalid",
                       errors=sorted(set(policy_errors + assignment_errors)))
    tests = sorted((row for row in assignments["observations"] if row["split"] == "test"),
                   key=lambda row: row["observation_id"])
    trains = sorted((row for row in assignments["observations"] if row["split"] == "train"),
                    key=lambda row: row["observation_id"])
    test_groups = {row["group_id"] for row in tests}
    embargo_days = int(policy["embargo_calendar_days"])
    purged: list[dict] = []
    kept: list[str] = []
    for train in trains:
        reasons: list[str] = []
        overlapping = [test["observation_id"] for test in tests if _overlap(train, test)]
        if overlapping:
            reasons.append("label_interval_overlap")
        if train["group_id"] in test_groups:
            reasons.append("shared_test_group")
        event_date = qf._parse_date(train["event_date"])
        embargo_tests = []
        for test in tests:
            test_end = qf._parse_date(test["label_end_date"])
            if (event_date is not None and test_end is not None and test_end < event_date
                    <= test_end + dt.timedelta(days=embargo_days)):
                embargo_tests.append(test["observation_id"])
        if embargo_tests:
            reasons.append("post_test_embargo")
        if reasons:
            purged.append({
                "observation_id": train["observation_id"],
                "reasons": sorted(reasons),
                "overlapping_test_observation_ids": overlapping,
                "embargo_test_observation_ids": embargo_tests,
            })
        else:
            kept.append(train["observation_id"])
    by_id = {row["observation_id"]: row for row in trains}
    retained_errors: list[str] = []
    for observation_id in kept:
        train = by_id[observation_id]
        if any(_overlap(train, test) for test in tests):
            retained_errors.append(f"retained_interval_overlap:{observation_id}")
        if train["group_id"] in test_groups:
            retained_errors.append(f"retained_shared_group:{observation_id}")
        event_date = qf._parse_date(train["event_date"])
        for test in tests:
            test_end = qf._parse_date(test["label_end_date"])
            if (event_date is not None and test_end is not None and test_end < event_date
                    <= test_end + dt.timedelta(days=embargo_days)):
                retained_errors.append(f"retained_embargo_violation:{observation_id}")
    if retained_errors:
        return _refuse("purge_independent_verification_failed", errors=retained_errors)
    document = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": PURGE_SCHEMA,
        "fold_id": assignments["fold_id"],
        "source_assignments_sha256": sha256_obj(assignments),
        "source_artifact_sha256": assignments["source_artifact_sha256"],
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "embargo_calendar_days": embargo_days,
        "test_observation_ids": [row["observation_id"] for row in tests],
        "kept_train_observation_ids": kept,
        "purged_train": purged,
        "counts": {
            "source_observation_count": len(assignments["observations"]),
            "test_count": len(tests),
            "source_train_count": len(trains),
            "kept_train_count": len(kept),
            "purged_train_count": len(purged),
            "interval_overlap_purge_count": sum(
                "label_interval_overlap" in row["reasons"] for row in purged),
            "group_purge_count": sum("shared_test_group" in row["reasons"] for row in purged),
            "embargo_purge_count": sum("post_test_embargo" in row["reasons"] for row in purged),
        },
        "leakage_free_after_purge": True,
        "retained_verification_errors": [],
        "audit_sha256": "",
    }
    document["audit_sha256"] = sha256_obj(_payload(document, "audit_sha256"))
    errors = validate_purge(document)
    if errors:
        return _refuse("purge_audit_self_validation_failed", errors=errors)
    return document


def validate_purge(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["purge_audit_not_object"]
    errors: list[str] = []
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"purge_audit_safety_invalid:{key}")
    if document.get("ok") is not True or document.get("schema") != PURGE_SCHEMA:
        errors.append("purge_audit_identity_invalid")
    if document.get("leakage_free_after_purge") is not True or document.get(
            "retained_verification_errors") != []:
        errors.append("purge_audit_leakage_claim_invalid")
    for key in ("source_assignments_sha256", "source_artifact_sha256", "policy_sha256",
                "audit_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                document.get(key, "")):
            errors.append(f"purge_audit_hash_invalid:{key}")
    counts = document.get("counts")
    kept = document.get("kept_train_observation_ids")
    purged = document.get("purged_train")
    tests = document.get("test_observation_ids")
    if not isinstance(counts, dict) or not isinstance(kept, list) or not isinstance(
            purged, list) or not isinstance(tests, list):
        errors.append("purge_audit_counts_or_lists_invalid")
    else:
        if counts.get("kept_train_count") != len(kept):
            errors.append("purge_audit_kept_count_mismatch")
        if counts.get("purged_train_count") != len(purged):
            errors.append("purge_audit_purged_count_mismatch")
        if counts.get("test_count") != len(tests):
            errors.append("purge_audit_test_count_mismatch")
        if counts.get("source_train_count") != len(kept) + len(purged):
            errors.append("purge_audit_train_denominator_mismatch")
        if counts.get("source_observation_count") != len(tests) + len(kept) + len(purged):
            errors.append("purge_audit_source_denominator_mismatch")
        purged_ids = [row.get("observation_id") for row in purged if isinstance(row, dict)]
        if (len(purged_ids) != len(purged) or len(set(purged_ids)) != len(purged_ids)
                or len(set(kept)) != len(kept) or len(set(tests)) != len(tests)
                or set(purged_ids) & set(kept) or set(purged_ids) & set(tests)
                or set(kept) & set(tests)):
            errors.append("purge_audit_partition_ids_invalid")
        allowed_reasons = {"label_interval_overlap", "shared_test_group", "post_test_embargo"}
        if any(not isinstance(row, dict) or not isinstance(row.get("reasons"), list)
               or not row["reasons"] or not set(row["reasons"]).issubset(allowed_reasons)
               for row in purged):
            errors.append("purge_audit_reason_invalid")
        expected_reason_counts = {
            "interval_overlap_purge_count": sum(
                "label_interval_overlap" in row.get("reasons", []) for row in purged),
            "group_purge_count": sum(
                "shared_test_group" in row.get("reasons", []) for row in purged),
            "embargo_purge_count": sum(
                "post_test_embargo" in row.get("reasons", []) for row in purged),
        }
        for key, expected in expected_reason_counts.items():
            if counts.get(key) != expected:
                errors.append(f"purge_audit_reason_count_mismatch:{key}")
    if document.get("audit_sha256") != sha256_obj(_payload(document, "audit_sha256")):
        errors.append("purge_audit_sha256_mismatch")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline dependence and purge diagnostics.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    portfolio = sub.add_parser("audit-portfolio")
    portfolio.add_argument("--portfolio", required=True)
    portfolio.add_argument("--policy", default=str(POLICY_DEFAULT))
    portfolio.add_argument("--out", required=True)
    intervals = sub.add_parser("audit-intervals")
    intervals.add_argument("--assignments", required=True)
    intervals.add_argument("--policy", default=str(POLICY_DEFAULT))
    intervals.add_argument("--out", required=True)
    validate_i = sub.add_parser("validate-inference")
    validate_i.add_argument("--artifact", required=True)
    validate_p = sub.add_parser("validate-purge")
    validate_p.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate-inference":
            document = _read(Path(args.artifact))
            errors = validate_inference(document)
            output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                      "audit_sha256": document.get("audit_sha256")}
        elif args.command == "validate-purge":
            document = _read(Path(args.artifact))
            errors = validate_purge(document)
            output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                      "audit_sha256": document.get("audit_sha256")}
        else:
            policy, policy_errors = load_policy(Path(args.policy))
            if policy is None or policy_errors:
                output = _refuse("inference_policy_invalid", errors=policy_errors)
            elif args.command == "audit-portfolio":
                output = audit_portfolio(_read(Path(args.portfolio)), policy)
            else:
                output = purge_and_embargo(_read(Path(args.assignments)), policy)
            if output.get("ok"):
                destination = Path(args.out)
                _atomic_write_once(destination, output)
                output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": True,
                          "written": str(destination),
                          "audit_sha256": output["audit_sha256"]}
        print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
        return 0 if output.get("ok") else 1
    except Exception as exc:
        print(json.dumps(_refuse(type(exc).__name__, detail=str(exc)[:400]),
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
