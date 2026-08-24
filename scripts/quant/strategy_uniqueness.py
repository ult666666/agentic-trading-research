#!/usr/bin/env python3
"""Offline strategy uniqueness and incremental-alpha diagnostics.

The audit compares one hash-valid, net-of-modeled-cost portfolio path with a benchmark and
the incumbent strategy library. It measures pairwise dependence, active-day overlap,
benchmark/incumbent-composite spanning with a Newey-West HAC intercept interval, and the
incremental utility of adding the candidate to an equal-weight incumbent portfolio.

This module is deliberately outside the research factory and production decision path. Its
strongest output is ``incremental_alpha_review_candidate``: a prompt for human category-D
review, never evidence of live profitability and never activation or execution authority.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import features as qf  # noqa: E402
from quant import portfolio_backtest as pb  # noqa: E402

TOOL = "quant.strategy_uniqueness"
POLICY_SCHEMA = "quant_strategy_uniqueness_policy/1.0"
RESULT_SCHEMA = "quant_strategy_uniqueness/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_uniqueness_policy.json"
_MAX_BYTES = 128 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NUM = (int, float)

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
}

OUTPUT_SAFETY = {
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
}

_POLICY_KEYS = {
    "schema", "version", "objective", "comparison_input",
    "minimum_aligned_observations", "minimum_candidate_active_observations",
    "minimum_common_date_coverage_fraction",
    "maximum_incumbents", "maximum_absolute_incumbent_correlation",
    "maximum_active_day_jaccard", "minimum_residual_alpha_lower_confidence_bound",
    "minimum_incremental_mean_daily_return_delta", "minimum_incremental_sharpe_delta",
    "minimum_incremental_max_drawdown_delta",
    "minimum_incremental_expected_shortfall_delta", "expected_shortfall_quantile",
    "confidence_level", "normal_critical_value", "hac_kernel", "hac_lag_rule",
    "incumbent_composite_rule", "augmented_portfolio_rule", "classification_order",
    "multiple_testing_policy", "safety",
}

_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "comparison_input": "hash_valid_quant_portfolio_backtest_daily_net_returns",
    "confidence_level": 0.95,
    "normal_critical_value": 1.9599639845,
    "hac_kernel": "bartlett",
    "hac_lag_rule": "floor_4_n_over_100_power_2_over_9_capped_n_minus_1",
    "incumbent_composite_rule": "equal_weight_daily_return",
    "augmented_portfolio_rule": "equal_weight_across_candidate_and_all_incumbents",
    "classification_order": [
        "insufficient_data",
        "redundant",
        "distinct_but_unproven",
        "incremental_alpha_review_candidate",
    ],
    "multiple_testing_policy": (
        "diagnostic_requires_separate_locked_holdout_dsr_pbo_and_category_D_review"),
}

LIMITATIONS = [
    "All returns are historical hypothetical portfolio diagnostics after modeled costs, not realized account performance.",
    "HAC inference assumes the aligned residual process is sufficiently weakly dependent and can fail under structural breaks.",
    "The equal-weight incumbent and augmented portfolios are fixed diagnostics; different capital-allocation rules can change the result.",
    "The residual-alpha interval is not a post-selection or multiple-testing correction; locked holdout, DSR, and PBO evidence remain separately required.",
    "Low correlation or low active-day overlap does not prove an economically independent causal edge.",
    "No result establishes future or live profitability, changes a strategy, creates an order, or grants activation authority.",
]

_SOURCE_KEYS = {
    "role", "strategy_id", "experiment_id", "variant_id", "result_sha256",
    "content_sha256", "source_observation_count", "first_date", "last_date",
    "cost_stress_multiplier", "cost_policy_version", "portfolio_policy_version",
    "cost_policy_sha256", "portfolio_policy_sha256",
}
_OBSERVATION_KEYS = {
    "date", "candidate_return", "benchmark_return", "incumbent_returns",
    "candidate_active", "incumbent_active",
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
    output = round(float(value), 12)
    return 0.0 if output == 0 else output


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _parse_as_of(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not _AS_OF_RE.fullmatch(value):
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


def _refuse(error: str, **extra: Any) -> dict:
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["uniqueness_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("uniqueness_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"uniqueness_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            document.get("version", "")):
        errors.append("uniqueness_policy_version_invalid")
    if not isinstance(document.get("objective"), str) or not document.get(
            "objective", "").strip():
        errors.append("uniqueness_policy_objective_invalid")

    integer_minima = {
        "minimum_aligned_observations": 60,
        "minimum_candidate_active_observations": 20,
    }
    for key, minimum in integer_minima.items():
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            errors.append(f"uniqueness_policy_minimum_too_weak:{key}")
    maximum_incumbents = document.get("maximum_incumbents")
    if (not isinstance(maximum_incumbents, int) or isinstance(maximum_incumbents, bool)
            or not 1 <= maximum_incumbents <= 20):
        errors.append("uniqueness_policy_maximum_incumbents_invalid")

    upper_bounds = {
        "maximum_absolute_incumbent_correlation": 0.8,
        "maximum_active_day_jaccard": 0.75,
        "expected_shortfall_quantile": 0.05,
    }
    for key, maximum in upper_bounds.items():
        value = document.get(key)
        if not _number(value) or not 0 < float(value) <= maximum:
            errors.append(f"uniqueness_policy_upper_bound_too_weak:{key}")
    lower_bounds = {
        "minimum_residual_alpha_lower_confidence_bound": 0.0,
        "minimum_incremental_mean_daily_return_delta": 0.0,
        "minimum_incremental_sharpe_delta": 0.05,
        "minimum_incremental_max_drawdown_delta": -0.02,
        "minimum_incremental_expected_shortfall_delta": -0.001,
        "minimum_common_date_coverage_fraction": 0.95,
    }
    for key, minimum in lower_bounds.items():
        value = document.get(key)
        if not _number(value) or float(value) < minimum:
            errors.append(f"uniqueness_policy_lower_bound_too_weak:{key}")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("uniqueness_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read(path)
    except Exception:
        return None, ["uniqueness_policy_unreadable"]
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
        "feeds_research_factory": False,
        "automatic_promotion_allowed": False,
    }


def hac_lag(n: int) -> int:
    if n <= 1:
        return 0
    return min(n - 1, max(0, math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))))


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    left_ss = sum((value - left_mean) ** 2 for value in left)
    right_ss = sum((value - right_mean) ** 2 for value in right)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return _round(sum((a - left_mean) * (b - right_mean)
                      for a, b in zip(left, right)) / math.sqrt(left_ss * right_ss))


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[cursor]]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[ordered[position]] = rank
        cursor = end
    return ranks


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    return _pearson(_ranks(left), _ranks(right))


def _jaccard(left: list[bool], right: list[bool]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    intersection = sum(a and b for a, b in zip(left, right))
    union = sum(a or b for a, b in zip(left, right))
    return None if union == 0 else _round(intersection / union)


def _matrix_inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    size = len(matrix)
    if size == 0 or any(len(row) != size for row in matrix):
        return None
    augmented = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(size)]
                 for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-14:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * pivot_value
                              for value, pivot_value in zip(
                                  augmented[row], augmented[column])]
    return [row[size:] for row in augmented]


def _matmul(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    columns = list(zip(*right))
    return [[sum(a * b for a, b in zip(row, column)) for column in columns]
            for row in left]


def _regression_unavailable(reason: str, n: int, lag: int) -> dict:
    return {
        "available": False,
        "reason": reason,
        "model": "candidate_return_on_intercept_benchmark_and_incumbent_composite",
        "observation_count": n,
        "hac_lag": lag,
        "coefficients": None,
        "intercept_hac_standard_error": None,
        "intercept_hac_t_statistic": None,
        "intercept_confidence_interval": None,
        "intercept_lower_bound_above_policy_threshold": None,
        "r_squared": None,
    }


def _hac_residual_alpha(candidate: list[float], benchmark: list[float],
                        incumbent_composite: list[float], policy: dict) -> dict:
    n = len(candidate)
    lag = hac_lag(n)
    if n < policy["minimum_aligned_observations"]:
        return _regression_unavailable("sample_below_minimum_aligned_observations", n, lag)
    if not (n == len(benchmark) == len(incumbent_composite)):
        return _regression_unavailable("regression_series_length_mismatch", n, lag)
    design = [[1.0, benchmark[index], incumbent_composite[index]] for index in range(n)]
    xtx = [[sum(row[i] * row[j] for row in design) for j in range(3)] for i in range(3)]
    inverse = _matrix_inverse(xtx)
    if inverse is None:
        return _regression_unavailable("factor_matrix_singular", n, lag)
    xty = [sum(row[i] * outcome for row, outcome in zip(design, candidate))
           for i in range(3)]
    coefficients = [sum(inverse[i][j] * xty[j] for j in range(3)) for i in range(3)]
    residuals = [outcome - sum(value * coefficient
                               for value, coefficient in zip(row, coefficients))
                 for row, outcome in zip(design, candidate)]
    if max(abs(value) for value in residuals) <= 1e-15:
        return _regression_unavailable("zero_residual_variance", n, lag)

    meat = [[0.0] * 3 for _ in range(3)]
    for row, residual in zip(design, residuals):
        for i in range(3):
            for j in range(3):
                meat[i][j] += residual * residual * row[i] * row[j]
    for offset in range(1, lag + 1):
        weight = 1.0 - offset / (lag + 1.0)
        for index in range(offset, n):
            current, previous = design[index], design[index - offset]
            covariance = residuals[index] * residuals[index - offset]
            for i in range(3):
                for j in range(3):
                    meat[i][j] += weight * covariance * (
                        current[i] * previous[j] + previous[i] * current[j])
    covariance_matrix = _matmul(_matmul(inverse, meat), inverse)
    intercept_variance = covariance_matrix[0][0]
    if intercept_variance <= 1e-24:
        return _regression_unavailable("nonpositive_intercept_hac_variance", n, lag)
    standard_error = math.sqrt(intercept_variance)
    intercept = coefficients[0]
    critical = float(policy["normal_critical_value"])
    lower = intercept - critical * standard_error
    upper = intercept + critical * standard_error
    mean = statistics.fmean(candidate)
    total_ss = sum((value - mean) ** 2 for value in candidate)
    residual_ss = sum(value * value for value in residuals)
    r_squared = None if total_ss <= 0 else 1.0 - residual_ss / total_ss
    return {
        "available": True,
        "reason": None,
        "model": "candidate_return_on_intercept_benchmark_and_incumbent_composite",
        "observation_count": n,
        "hac_lag": lag,
        "coefficients": {
            "intercept_per_observation": _round(intercept),
            "benchmark_beta": _round(coefficients[1]),
            "incumbent_composite_beta": _round(coefficients[2]),
        },
        "intercept_hac_standard_error": _round(standard_error),
        "intercept_hac_t_statistic": _round(intercept / standard_error),
        "intercept_confidence_interval": [_round(lower), _round(upper)],
        "intercept_lower_bound_above_policy_threshold": (
            lower > float(policy["minimum_residual_alpha_lower_confidence_bound"])),
        "r_squared": _round(r_squared),
    }


def _portfolio_metrics(returns: list[float], quantile: float) -> dict:
    if not returns:
        return {
            "mean_daily_return": None,
            "sharpe_per_observation": None,
            "maximum_drawdown": None,
            "expected_shortfall": None,
        }
    mean = statistics.fmean(returns)
    deviation = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    nav = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        nav *= 1.0 + value
        peak = max(peak, nav)
        maximum_drawdown = min(maximum_drawdown, nav / peak - 1.0)
    tail_count = max(1, math.ceil(len(returns) * quantile))
    expected_shortfall = statistics.fmean(sorted(returns)[:tail_count])
    return {
        "mean_daily_return": _round(mean),
        "sharpe_per_observation": None if deviation <= 0 else _round(mean / deviation),
        "maximum_drawdown": _round(maximum_drawdown),
        "expected_shortfall": _round(expected_shortfall),
    }


def _delta(augmented: Any, baseline: Any) -> float | None:
    if not _number(augmented) or not _number(baseline):
        return None
    return _round(float(augmented) - float(baseline))


def _gate(passed: bool, observed: Any, threshold: Any, reason: str) -> dict:
    return {
        "passed": bool(passed),
        "observed": observed,
        "threshold": threshold,
        "reason": reason,
    }


def _derive(observations: list[dict], policy: dict, candidate_source_unique: bool,
            source_contract_compatible: bool,
            minimum_common_date_coverage_fraction: float | None) -> dict:
    incumbent_ids = sorted(observations[0]["incumbent_returns"]) if observations else []
    candidate = [float(row["candidate_return"]) for row in observations]
    benchmark = [float(row["benchmark_return"]) for row in observations]
    incumbents = {
        strategy_id: [float(row["incumbent_returns"][strategy_id]) for row in observations]
        for strategy_id in incumbent_ids
    }
    incumbent_composite = [
        statistics.fmean(row["incumbent_returns"].values())
        for row in observations
    ] if incumbent_ids else []

    incumbent_dependence = []
    for strategy_id in incumbent_ids:
        incumbent_dependence.append({
            "strategy_id": strategy_id,
            "pearson_return_correlation": _pearson(candidate, incumbents[strategy_id]),
            "spearman_return_correlation": _spearman(candidate, incumbents[strategy_id]),
            "active_day_jaccard": _jaccard(
                [bool(row["candidate_active"]) for row in observations],
                [bool(row["incumbent_active"][strategy_id]) for row in observations]),
        })
    correlations = [abs(float(row["pearson_return_correlation"]))
                    for row in incumbent_dependence
                    if row["pearson_return_correlation"] is not None]
    overlaps = [float(row["active_day_jaccard"]) for row in incumbent_dependence
                if row["active_day_jaccard"] is not None]
    pairwise = {
        "benchmark": {
            "strategy_role": "benchmark",
            "pearson_return_correlation": _pearson(candidate, benchmark),
            "spearman_return_correlation": _spearman(candidate, benchmark),
        },
        "incumbents": incumbent_dependence,
        "maximum_absolute_incumbent_pearson_correlation": (
            _round(max(correlations)) if correlations else None),
        "maximum_incumbent_active_day_jaccard": (
            _round(max(overlaps)) if overlaps else None),
    }

    residual = _hac_residual_alpha(candidate, benchmark, incumbent_composite, policy)
    baseline = _portfolio_metrics(incumbent_composite,
                                  float(policy["expected_shortfall_quantile"]))
    augmented_returns = [
        (row["candidate_return"] + sum(row["incumbent_returns"].values()))
        / (len(incumbent_ids) + 1)
        for row in observations
    ] if incumbent_ids else []
    augmented = _portfolio_metrics(augmented_returns,
                                   float(policy["expected_shortfall_quantile"]))
    incremental = {
        "baseline_rule": policy["incumbent_composite_rule"],
        "augmented_rule": policy["augmented_portfolio_rule"],
        "baseline_incumbent_portfolio": baseline,
        "augmented_with_candidate_portfolio": augmented,
        "deltas_augmented_minus_baseline": {
            "mean_daily_return": _delta(augmented["mean_daily_return"],
                                        baseline["mean_daily_return"]),
            "sharpe_per_observation": _delta(augmented["sharpe_per_observation"],
                                             baseline["sharpe_per_observation"]),
            "maximum_drawdown": _delta(augmented["maximum_drawdown"],
                                       baseline["maximum_drawdown"]),
            "expected_shortfall": _delta(augmented["expected_shortfall"],
                                         baseline["expected_shortfall"]),
        },
    }

    aligned_count = len(observations)
    active_count = sum(bool(row["candidate_active"]) for row in observations)
    max_correlation = pairwise["maximum_absolute_incumbent_pearson_correlation"]
    max_overlap = pairwise["maximum_incumbent_active_day_jaccard"]
    deltas = incremental["deltas_augmented_minus_baseline"]
    residual_interval = residual.get("intercept_confidence_interval")
    residual_lower = residual_interval[0] if isinstance(residual_interval, list) else None
    gate_results = {
        "minimum_aligned_observations": _gate(
            aligned_count >= policy["minimum_aligned_observations"], aligned_count,
            {"minimum": policy["minimum_aligned_observations"]},
            "common-date denominator must be large enough for review"),
        "minimum_candidate_active_observations": _gate(
            active_count >= policy["minimum_candidate_active_observations"], active_count,
            {"minimum": policy["minimum_candidate_active_observations"]},
            "candidate must take risk often enough to evaluate"),
        "minimum_common_date_coverage_fraction": _gate(
            minimum_common_date_coverage_fraction is not None
            and minimum_common_date_coverage_fraction >= policy[
                "minimum_common_date_coverage_fraction"],
            _round(minimum_common_date_coverage_fraction),
            {"minimum": policy["minimum_common_date_coverage_fraction"]},
            "the common-date intersection must cover nearly all observations in every source"),
        "source_contract_compatible": _gate(
            source_contract_compatible, source_contract_compatible, {"required": True},
            "cost stress, cost policy, and portfolio policy must match across every source"),
        "candidate_source_unique": _gate(
            candidate_source_unique, candidate_source_unique, {"required": True},
            "candidate result hash must differ from benchmark and every incumbent"),
        "incumbent_correlations_available": _gate(
            len(correlations) == len(incumbent_ids) and bool(incumbent_ids),
            len(correlations), {"required": len(incumbent_ids)},
            "constant or missing incumbent return paths cannot establish uniqueness"),
        "maximum_absolute_incumbent_correlation": _gate(
            max_correlation is not None and max_correlation <= policy[
                "maximum_absolute_incumbent_correlation"],
            max_correlation,
            {"maximum": policy["maximum_absolute_incumbent_correlation"]},
            "high return correlation indicates an incumbent clone"),
        "active_day_overlap_available": _gate(
            len(overlaps) == len(incumbent_ids) and bool(incumbent_ids), len(overlaps),
            {"required": len(incumbent_ids)},
            "active-day overlap must be measurable for every incumbent"),
        "maximum_active_day_jaccard": _gate(
            max_overlap is not None and max_overlap <= policy["maximum_active_day_jaccard"],
            max_overlap, {"maximum": policy["maximum_active_day_jaccard"]},
            "high active-day overlap indicates duplicated timing"),
        "residual_alpha_available": _gate(
            residual["available"], residual["available"], {"required": True},
            residual["reason"] or "HAC spanning regression is estimable"),
        "residual_alpha_lower_confidence_bound": _gate(
            bool(residual.get("intercept_lower_bound_above_policy_threshold")),
            residual_lower,
            {"strictly_greater_than": policy[
                "minimum_residual_alpha_lower_confidence_bound"]},
            "candidate intercept must remain positive after benchmark and incumbent spanning"),
        "incremental_sharpe_available": _gate(
            deltas["sharpe_per_observation"] is not None,
            deltas["sharpe_per_observation"], {"required": "finite"},
            "baseline and augmented portfolio Sharpe diagnostics must be estimable"),
        "minimum_incremental_mean_daily_return_delta": _gate(
            deltas["mean_daily_return"] is not None and deltas[
                "mean_daily_return"] >= policy[
                    "minimum_incremental_mean_daily_return_delta"],
            deltas["mean_daily_return"],
            {"minimum": policy["minimum_incremental_mean_daily_return_delta"]},
            "adding the candidate must not dilute mean daily return"),
        "minimum_incremental_sharpe_delta": _gate(
            deltas["sharpe_per_observation"] is not None and deltas[
                "sharpe_per_observation"] >= policy["minimum_incremental_sharpe_delta"],
            deltas["sharpe_per_observation"],
            {"minimum": policy["minimum_incremental_sharpe_delta"]},
            "adding the candidate must improve per-observation risk-adjusted return"),
        "minimum_incremental_max_drawdown_delta": _gate(
            deltas["maximum_drawdown"] is not None and deltas[
                "maximum_drawdown"] >= policy[
                    "minimum_incremental_max_drawdown_delta"],
            deltas["maximum_drawdown"],
            {"minimum": policy["minimum_incremental_max_drawdown_delta"]},
            "adding the candidate may not materially worsen historical drawdown"),
        "minimum_incremental_expected_shortfall_delta": _gate(
            deltas["expected_shortfall"] is not None and deltas[
                "expected_shortfall"] >= policy[
                    "minimum_incremental_expected_shortfall_delta"],
            deltas["expected_shortfall"],
            {"minimum": policy["minimum_incremental_expected_shortfall_delta"]},
            "adding the candidate may not materially worsen historical left-tail return"),
    }

    identity_availability = (
        "minimum_aligned_observations",
        "minimum_candidate_active_observations",
        "minimum_common_date_coverage_fraction",
        "source_contract_compatible",
        "incumbent_correlations_available",
        "active_day_overlap_available",
    )
    inference_availability = (
        "residual_alpha_available",
        "incremental_sharpe_available",
    )
    redundancy = (
        "candidate_source_unique",
        "maximum_absolute_incumbent_correlation",
        "maximum_active_day_jaccard",
    )
    if any(not gate_results[key]["passed"] for key in identity_availability):
        classification = "insufficient_data"
    elif any(not gate_results[key]["passed"] for key in redundancy):
        classification = "redundant"
    elif any(not gate_results[key]["passed"] for key in inference_availability):
        classification = "insufficient_data"
    elif all(item["passed"] for item in gate_results.values()):
        classification = "incremental_alpha_review_candidate"
    else:
        classification = "distinct_but_unproven"
    return {
        "pairwise_dependence": pairwise,
        "residual_alpha": residual,
        "incremental_portfolio": incremental,
        "gate_results": gate_results,
        "classification": classification,
    }


def _portfolio_errors(document: Any, role: str) -> list[str]:
    errors = [f"{role}:{value}" for value in pb.validate_result(document)]
    if not isinstance(document, dict):
        return errors
    for key in ("experiment_id", "variant_id"):
        value = document.get(key)
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            errors.append(f"{role}:{key}_invalid")
    if not _number(document.get("cost_stress_multiplier")) or float(
            document.get("cost_stress_multiplier", 0)) < 1:
        errors.append(f"{role}:cost_stress_multiplier_invalid")
    versions = document.get("policy_versions")
    if not isinstance(versions, dict):
        errors.append(f"{role}:policy_versions_invalid")
    else:
        for key in ("cost", "portfolio"):
            if not isinstance(versions.get(key), str) or not versions.get(key):
                errors.append(f"{role}:policy_version_invalid:{key}")
    lineage = document.get("lineage")
    if not isinstance(lineage, dict):
        errors.append(f"{role}:lineage_invalid")
    else:
        for key in ("cost_policy_sha256", "portfolio_policy_sha256"):
            if not isinstance(lineage.get(key), str) or not _SHA_RE.fullmatch(
                    lineage.get(key, "")):
                errors.append(f"{role}:lineage_contract_hash_invalid:{key}")
    daily = document.get("daily_path")
    if isinstance(daily, list):
        for index, row in enumerate(daily):
            if not isinstance(row, dict):
                continue
            value = row.get("daily_return")
            if not _number(value) or float(value) <= -1:
                errors.append(f"{role}:daily_return_invalid:{index}")
            exposure = row.get("gross_exposure")
            if not _number(exposure) or float(exposure) < 0:
                errors.append(f"{role}:gross_exposure_invalid:{index}")
    return sorted(set(errors))


def _series(document: dict) -> dict[str, dict]:
    return {
        row["date"]: {
            "return": float(row["daily_return"]),
            "active": float(row["gross_exposure"]) > 1e-12,
        }
        for row in document["daily_path"]
    }


def _source_metadata(document: dict, role: str, strategy_id: str) -> dict:
    daily = document["daily_path"]
    return {
        "role": role,
        "strategy_id": strategy_id,
        "experiment_id": document["experiment_id"],
        "variant_id": document["variant_id"],
        "result_sha256": document["result_sha256"],
        "content_sha256": sha256_obj(document),
        "source_observation_count": len(daily),
        "first_date": daily[0]["date"],
        "last_date": daily[-1]["date"],
        "cost_stress_multiplier": float(document["cost_stress_multiplier"]),
        "cost_policy_version": document["policy_versions"]["cost"],
        "portfolio_policy_version": document["policy_versions"]["portfolio"],
        "cost_policy_sha256": document["lineage"]["cost_policy_sha256"],
        "portfolio_policy_sha256": document["lineage"]["portfolio_policy_sha256"],
    }


def _source_contract_key(source: dict) -> tuple:
    return (
        source.get("cost_stress_multiplier"),
        source.get("cost_policy_version"),
        source.get("portfolio_policy_version"),
        source.get("cost_policy_sha256"),
        source.get("portfolio_policy_sha256"),
    )


def _default_id(role: str, document: dict) -> str:
    value = f"{role}:{document.get('variant_id', 'unknown')}"
    return value[:128]


def audit_portfolios(candidate: Any, benchmark: Any, incumbents: dict[str, Any],
                     policy: dict, as_of_utc: str, candidate_id: str | None = None,
                     benchmark_id: str | None = None) -> dict:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return _refuse("uniqueness_policy_invalid", errors=policy_errors)
    as_of = _parse_as_of(as_of_utc)
    if as_of is None:
        return _refuse("as_of_utc_invalid")
    if not isinstance(incumbents, dict) or not 1 <= len(incumbents) <= policy[
            "maximum_incumbents"]:
        return _refuse("incumbent_count_invalid")
    candidate_id = candidate_id or (_default_id("candidate", candidate)
                                     if isinstance(candidate, dict) else "candidate:unknown")
    benchmark_id = benchmark_id or (_default_id("benchmark", benchmark)
                                     if isinstance(benchmark, dict) else "benchmark:unknown")
    all_ids = [candidate_id, benchmark_id, *incumbents]
    if any(not isinstance(value, str) or not _ID_RE.fullmatch(value) for value in all_ids):
        return _refuse("strategy_id_invalid")
    if len(set(all_ids)) != len(all_ids):
        return _refuse("strategy_id_duplicate")

    errors = _portfolio_errors(candidate, "candidate")
    errors.extend(_portfolio_errors(benchmark, "benchmark"))
    for strategy_id, document in incumbents.items():
        errors.extend(_portfolio_errors(document, f"incumbent:{strategy_id}"))
    if errors:
        return _refuse("portfolio_sources_invalid", errors=sorted(set(errors)))

    source_documents = [candidate, benchmark, *incumbents.values()]
    if any(dt.date.fromisoformat(document["daily_path"][-1]["date"]) > as_of.date()
           for document in source_documents):
        return _refuse("source_observation_after_as_of")

    candidate_series = _series(candidate)
    benchmark_series = _series(benchmark)
    incumbent_series = {key: _series(value) for key, value in incumbents.items()}
    common_dates = set(candidate_series) & set(benchmark_series)
    for values in incumbent_series.values():
        common_dates &= set(values)
    aligned_dates = sorted(common_dates)
    incumbent_ids = sorted(incumbents)
    observations = [
        {
            "date": date,
            "candidate_return": candidate_series[date]["return"],
            "benchmark_return": benchmark_series[date]["return"],
            "incumbent_returns": {
                strategy_id: incumbent_series[strategy_id][date]["return"]
                for strategy_id in incumbent_ids
            },
            "candidate_active": candidate_series[date]["active"],
            "incumbent_active": {
                strategy_id: incumbent_series[strategy_id][date]["active"]
                for strategy_id in incumbent_ids
            },
        }
        for date in aligned_dates
    ]
    sources = {
        "candidate": _source_metadata(candidate, "candidate", candidate_id),
        "benchmark": _source_metadata(benchmark, "benchmark", benchmark_id),
        "incumbents": [
            _source_metadata(incumbents[strategy_id], "incumbent", strategy_id)
            for strategy_id in incumbent_ids
        ],
    }
    source_rows = [sources["benchmark"], *sources["incumbents"]]
    candidate_source_unique = all(
        sources["candidate"]["result_sha256"] != row["result_sha256"]
        for row in source_rows)
    source_contract_compatible = all(
        _source_contract_key(row) == _source_contract_key(sources["candidate"])
        for row in source_rows)
    counts = {
        candidate_id: len(candidate_series),
        benchmark_id: len(benchmark_series),
        **{strategy_id: len(incumbent_series[strategy_id]) for strategy_id in incumbent_ids},
    }
    coverage_fractions = {
        key: _round(len(observations) / value) for key, value in sorted(counts.items())
    }
    minimum_coverage_fraction = min(coverage_fractions.values()) if coverage_fractions else None
    derived = _derive(
        observations,
        policy,
        candidate_source_unique,
        source_contract_compatible,
        minimum_coverage_fraction,
    )
    coverage = {
        "source_observation_counts": counts,
        "aligned_observation_count": len(observations),
        "candidate_active_observation_count": sum(
            row["candidate_active"] for row in observations),
        "dropped_outside_common_date_intersection": {
            key: value - len(observations) for key, value in sorted(counts.items())
        },
        "common_date_coverage_fraction": coverage_fractions,
        "minimum_common_date_coverage_fraction": minimum_coverage_fraction,
        "first_aligned_date": aligned_dates[0] if aligned_dates else None,
        "last_aligned_date": aligned_dates[-1] if aligned_dates else None,
    }
    result = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": RESULT_SCHEMA,
        "as_of_utc": as_of_utc,
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "policy_snapshot": policy,
        "source_artifacts": sources,
        "source_set_sha256": sha256_obj(sources),
        "coverage": coverage,
        "aligned_observations": observations,
        "aligned_observations_sha256": sha256_obj(observations),
        **derived,
        "review_status": "diagnostic_only_no_activation_authority",
        "multiple_testing_status": (
            "not_recomputed_requires_separate_locked_holdout_dsr_pbo"),
        "profitability_status": "not_established",
        "limitations": LIMITATIONS,
        "actual_orders_count": 0,
        "result_sha256": "",
    }
    result["result_sha256"] = sha256_obj(_payload(result, "result_sha256"))
    result_errors = validate_result(result)
    if result_errors:
        return _refuse("uniqueness_result_self_validation_failed", errors=result_errors)
    return result


def _validate_source(source: Any, expected_role: str, tag: str) -> list[str]:
    if not isinstance(source, dict):
        return [f"uniqueness_source_not_object:{tag}"]
    errors: list[str] = []
    if set(source) != _SOURCE_KEYS:
        errors.append(f"uniqueness_source_keys_invalid:{tag}")
    if source.get("role") != expected_role:
        errors.append(f"uniqueness_source_role_invalid:{tag}")
    for key in ("strategy_id", "experiment_id", "variant_id"):
        if not isinstance(source.get(key), str) or not _ID_RE.fullmatch(source.get(key, "")):
            errors.append(f"uniqueness_source_id_invalid:{tag}:{key}")
    for key in ("result_sha256", "content_sha256"):
        if not isinstance(source.get(key), str) or not _SHA_RE.fullmatch(source.get(key, "")):
            errors.append(f"uniqueness_source_hash_invalid:{tag}:{key}")
    if not _number(source.get("cost_stress_multiplier")) or float(
            source.get("cost_stress_multiplier", 0)) < 1:
        errors.append(f"uniqueness_source_cost_stress_invalid:{tag}")
    for key in ("cost_policy_version", "portfolio_policy_version"):
        if not isinstance(source.get(key), str) or not source.get(key):
            errors.append(f"uniqueness_source_policy_version_invalid:{tag}:{key}")
    for key in ("cost_policy_sha256", "portfolio_policy_sha256"):
        if not isinstance(source.get(key), str) or not _SHA_RE.fullmatch(source.get(key, "")):
            errors.append(f"uniqueness_source_contract_hash_invalid:{tag}:{key}")
    count = source.get("source_observation_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        errors.append(f"uniqueness_source_count_invalid:{tag}")
    if not _valid_date(source.get("first_date")) or not _valid_date(source.get("last_date")):
        errors.append(f"uniqueness_source_date_invalid:{tag}")
    elif source["first_date"] > source["last_date"]:
        errors.append(f"uniqueness_source_date_order_invalid:{tag}")
    return errors


def validate_result(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["uniqueness_result_not_object"]
    errors: list[str] = []
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"uniqueness_result_safety_invalid:{key}")
    if (document.get("tool") != TOOL or document.get("ok") is not True
            or document.get("schema") != RESULT_SCHEMA):
        errors.append("uniqueness_result_identity_invalid")
    as_of = _parse_as_of(document.get("as_of_utc"))
    if as_of is None:
        errors.append("uniqueness_result_as_of_invalid")

    policy = document.get("policy_snapshot")
    policy_errors = validate_policy(policy)
    errors.extend(f"embedded_{value}" for value in policy_errors)
    if isinstance(policy, dict):
        if document.get("policy_version") != policy.get("version"):
            errors.append("uniqueness_policy_version_mismatch")
        if document.get("policy_sha256") != sha256_obj(policy):
            errors.append("uniqueness_policy_sha256_mismatch")

    sources = document.get("source_artifacts")
    source_ids: list[str] = []
    incumbent_ids: list[str] = []
    source_rows: list[dict] = []
    if not isinstance(sources, dict) or set(sources) != {
            "candidate", "benchmark", "incumbents"}:
        errors.append("uniqueness_sources_invalid")
    else:
        errors.extend(_validate_source(sources.get("candidate"), "candidate", "candidate"))
        errors.extend(_validate_source(sources.get("benchmark"), "benchmark", "benchmark"))
        incumbents = sources.get("incumbents")
        if not isinstance(incumbents, list) or not incumbents:
            errors.append("uniqueness_incumbent_sources_invalid")
            incumbents = []
        for index, source in enumerate(incumbents):
            errors.extend(_validate_source(source, "incumbent", f"incumbent:{index}"))
        source_rows = [row for row in [sources.get("candidate"), sources.get("benchmark"),
                                      *incumbents] if isinstance(row, dict)]
        source_ids = [row.get("strategy_id") for row in source_rows
                      if isinstance(row.get("strategy_id"), str)]
        incumbent_ids = [row.get("strategy_id") for row in incumbents
                         if isinstance(row, dict) and isinstance(row.get("strategy_id"), str)]
        if incumbent_ids != sorted(incumbent_ids) or len(set(source_ids)) != len(source_ids):
            errors.append("uniqueness_source_ids_not_unique_sorted")
        if isinstance(policy, dict) and len(incumbents) > policy.get("maximum_incumbents", 0):
            errors.append("uniqueness_incumbent_count_exceeds_policy")
        if as_of is not None:
            for row in source_rows:
                if (_valid_date(row.get("last_date"))
                        and dt.date.fromisoformat(row["last_date"]) > as_of.date()):
                    errors.append(
                        f"uniqueness_source_after_as_of:{row.get('strategy_id')}")
        if document.get("source_set_sha256") != sha256_obj(sources):
            errors.append("uniqueness_source_set_sha256_mismatch")

    observations = document.get("aligned_observations")
    if not isinstance(observations, list):
        errors.append("uniqueness_observations_not_list")
        observations = []
    previous_date = None
    for index, row in enumerate(observations):
        if not isinstance(row, dict) or set(row) != _OBSERVATION_KEYS:
            errors.append(f"uniqueness_observation_invalid:{index}")
            continue
        date = row.get("date")
        if not _valid_date(date):
            errors.append(f"uniqueness_observation_date_invalid:{index}")
        elif previous_date is not None and date <= previous_date:
            errors.append(f"uniqueness_observation_dates_not_increasing:{index}")
        else:
            previous_date = date
            if as_of is not None and dt.date.fromisoformat(date) > as_of.date():
                errors.append(f"uniqueness_observation_after_as_of:{index}")
        for key in ("candidate_return", "benchmark_return"):
            if not _number(row.get(key)) or float(row.get(key, -1)) <= -1:
                errors.append(f"uniqueness_observation_return_invalid:{index}:{key}")
        for key in ("incumbent_returns", "incumbent_active"):
            value = row.get(key)
            if not isinstance(value, dict) or sorted(value) != incumbent_ids:
                errors.append(f"uniqueness_observation_incumbents_invalid:{index}:{key}")
        if isinstance(row.get("incumbent_returns"), dict):
            for key, value in row["incumbent_returns"].items():
                if not _number(value) or float(value) <= -1:
                    errors.append(f"uniqueness_incumbent_return_invalid:{index}:{key}")
        if type(row.get("candidate_active")) is not bool:
            errors.append(f"uniqueness_candidate_active_invalid:{index}")
        if isinstance(row.get("incumbent_active"), dict) and any(
                type(value) is not bool for value in row["incumbent_active"].values()):
            errors.append(f"uniqueness_incumbent_active_invalid:{index}")
    if document.get("aligned_observations_sha256") != sha256_obj(observations):
        errors.append("uniqueness_observations_sha256_mismatch")

    coverage = document.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("uniqueness_coverage_invalid")
    else:
        expected_count = len(observations)
        if coverage.get("aligned_observation_count") != expected_count:
            errors.append("uniqueness_coverage_aligned_count_mismatch")
        if coverage.get("candidate_active_observation_count") != sum(
                bool(row.get("candidate_active")) for row in observations
                if isinstance(row, dict)):
            errors.append("uniqueness_coverage_active_count_mismatch")
        expected_first = observations[0].get("date") if observations else None
        expected_last = observations[-1].get("date") if observations else None
        if coverage.get("first_aligned_date") != expected_first:
            errors.append("uniqueness_coverage_first_date_mismatch")
        if coverage.get("last_aligned_date") != expected_last:
            errors.append("uniqueness_coverage_last_date_mismatch")
        counts = coverage.get("source_observation_counts")
        dropped = coverage.get("dropped_outside_common_date_intersection")
        fractions = coverage.get("common_date_coverage_fraction")
        expected_ids = sorted(source_ids)
        if not isinstance(counts, dict) or sorted(counts) != expected_ids:
            errors.append("uniqueness_coverage_source_counts_invalid")
        if not isinstance(dropped, dict) or sorted(dropped) != expected_ids:
            errors.append("uniqueness_coverage_dropped_counts_invalid")
        if not isinstance(fractions, dict) or sorted(fractions) != expected_ids:
            errors.append("uniqueness_coverage_fractions_invalid")
        if isinstance(counts, dict) and isinstance(dropped, dict):
            for strategy_id in expected_ids:
                count = counts.get(strategy_id)
                if (not isinstance(count, int) or isinstance(count, bool)
                        or count < expected_count):
                    errors.append(f"uniqueness_coverage_source_count_invalid:{strategy_id}")
                elif dropped.get(strategy_id) != count - expected_count:
                    errors.append(f"uniqueness_coverage_dropped_mismatch:{strategy_id}")
                if (isinstance(fractions, dict) and isinstance(count, int) and count > 0
                        and fractions.get(strategy_id) != _round(expected_count / count)):
                    errors.append(f"uniqueness_coverage_fraction_mismatch:{strategy_id}")
        valid_fractions = (isinstance(fractions, dict) and bool(fractions)
                           and all(_number(value) and 0 <= float(value) <= 1
                                   for value in fractions.values()))
        if isinstance(fractions, dict) and not valid_fractions:
            errors.append("uniqueness_coverage_fraction_values_invalid")
        expected_minimum_fraction = min(fractions.values()) if valid_fractions else None
        if coverage.get("minimum_common_date_coverage_fraction") != expected_minimum_fraction:
            errors.append("uniqueness_coverage_minimum_fraction_mismatch")
        metadata_counts = {row.get("strategy_id"): row.get("source_observation_count")
                           for row in source_rows}
        if isinstance(counts, dict) and counts != metadata_counts:
            errors.append("uniqueness_coverage_metadata_count_mismatch")

    candidate_unique = False
    source_contract_compatible = False
    if isinstance(sources, dict) and isinstance(sources.get("candidate"), dict):
        comparison_rows = []
        if isinstance(sources.get("benchmark"), dict):
            comparison_rows.append(sources["benchmark"])
        if isinstance(sources.get("incumbents"), list):
            comparison_rows.extend(row for row in sources["incumbents"]
                                   if isinstance(row, dict))
        candidate_unique = all(sources["candidate"].get("result_sha256") != row.get(
            "result_sha256") for row in comparison_rows)
        source_contract_compatible = all(
            _source_contract_key(row) == _source_contract_key(sources["candidate"])
            for row in comparison_rows)
    if isinstance(policy, dict) and not policy_errors:
        try:
            minimum_fraction = (coverage.get("minimum_common_date_coverage_fraction")
                                if isinstance(coverage, dict) else None)
            expected = _derive(observations, policy, candidate_unique,
                               source_contract_compatible, minimum_fraction)
        except Exception:
            errors.append("uniqueness_semantic_recomputation_failed")
        else:
            for key in ("pairwise_dependence", "residual_alpha", "incremental_portfolio",
                        "gate_results", "classification"):
                if document.get(key) != expected[key]:
                    errors.append(f"uniqueness_semantic_mismatch:{key}")

    fixed = {
        "review_status": "diagnostic_only_no_activation_authority",
        "multiple_testing_status": (
            "not_recomputed_requires_separate_locked_holdout_dsr_pbo"),
        "profitability_status": "not_established",
        "limitations": LIMITATIONS,
        "actual_orders_count": 0,
    }
    for key, expected in fixed.items():
        if document.get(key) != expected:
            errors.append(f"uniqueness_fixed_field_invalid:{key}")
    if document.get("result_sha256") != sha256_obj(_payload(document, "result_sha256")):
        errors.append("uniqueness_result_sha256_mismatch")
    return sorted(set(errors))


def _incumbent_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError("incumbent_argument_must_be_ID_equals_PATH")
    strategy_id, raw_path = value.split("=", 1)
    if not _ID_RE.fullmatch(strategy_id) or not raw_path:
        raise ValueError("incumbent_argument_invalid")
    return strategy_id, Path(raw_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline strategy uniqueness and incremental-alpha diagnostics.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    audit = sub.add_parser("audit")
    audit.add_argument("--candidate", required=True)
    audit.add_argument("--benchmark", required=True)
    audit.add_argument("--incumbent", action="append", required=True,
                       metavar="ID=PATH")
    audit.add_argument("--candidate-id")
    audit.add_argument("--benchmark-id")
    audit.add_argument("--as-of", required=True)
    audit.add_argument("--policy", default=str(POLICY_DEFAULT))
    audit.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate":
            document = _read(Path(args.artifact))
            errors = validate_result(document)
            output = {
                **OUTPUT_SAFETY,
                "tool": TOOL,
                "ok": not errors,
                "errors": errors,
                "classification": document.get("classification"),
                "result_sha256": document.get("result_sha256"),
            }
        else:
            policy, policy_errors = load_policy(Path(args.policy))
            if policy is None or policy_errors:
                output = _refuse("uniqueness_policy_invalid", errors=policy_errors)
            else:
                parsed = [_incumbent_arg(value) for value in args.incumbent]
                if len({key for key, _ in parsed}) != len(parsed):
                    raise ValueError("incumbent_id_duplicate")
                incumbent_documents = {key: _read(path) for key, path in parsed}
                output = audit_portfolios(
                    _read(Path(args.candidate)),
                    _read(Path(args.benchmark)),
                    incumbent_documents,
                    policy,
                    args.as_of,
                    args.candidate_id,
                    args.benchmark_id,
                )
                if output.get("ok"):
                    _atomic_write_once(Path(args.out), output)
                    output = {
                        **OUTPUT_SAFETY,
                        "tool": TOOL,
                        "ok": True,
                        "classification": output["classification"],
                        "aligned_observation_count": output[
                            "coverage"]["aligned_observation_count"],
                        "result_sha256": output["result_sha256"],
                        "output": str(Path(args.out)),
                    }
    except Exception as exc:
        output = _refuse(f"{type(exc).__name__}:{exc}")
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
