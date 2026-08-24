#!/usr/bin/env python3
"""Offline Hansen Superior Predictive Ability audit for complete portfolio families.

The tool builds an aligned daily-return panel from validated normalized-capital portfolio
backtests and applies Hansen's studentized stationary-bootstrap SPA test. It has no production,
strategy-promotion, Buy, broker, account, model, network, or execution authority.
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

TOOL = "quant.spa_audit"
VERSION = "1.1.0"
POLICY_SCHEMA = "quant_spa_policy/1.0"
PANEL_SCHEMA = "quant_spa_panel/1.0"
AUDIT_SCHEMA = "quant_spa_audit/1.0"
PORTFOLIO_SCHEMA = "quant_portfolio_backtest/1.0"
DEFAULT_POLICY = ROOT / "config" / "quant_spa_policy.json"
POLICY_SCHEMA_PATH = ROOT / "schemas" / "quant_spa_policy.schema.json"
PANEL_SCHEMA_PATH = ROOT / "schemas" / "quant_spa_panel.schema.json"
AUDIT_SCHEMA_PATH = ROOT / "schemas" / "quant_spa_audit.schema.json"
UTC = dt.timezone.utc
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)
_MAX_INPUT_BYTES = 128 * 1024 * 1024
_EPS = 1e-14

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "feeds_research_factory": False,
    "feeds_buy_gate": False,
    "strategy_change_allowed": False,
    "promotion_allowed": False,
    "profitability_claim_allowed": False,
    "counts_in_metrics": False,
    "broker_access": "none",
    "network_invoked": False,
    "model_invoked": False,
    "account_data_allowed": False,
}
OUTPUT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "account_data_allowed": False,
    "network_invoked": False,
    "model_invoked": False,
    "promotion_allowed": False,
    "profitability_claim_allowed": False,
    "feeds_research_factory": False,
    "feeds_buy_gate": False,
    "counts_in_metrics": False,
}
OFFICIAL_SOURCES = [
    "https://doi.org/10.1198/073500105000000063",
    "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=264569",
    "https://doi.org/10.1111/1468-0262.00152",
]
EXPECTED_POLICY = {
    "schema": POLICY_SCHEMA,
    "version": VERSION,
    "panel_schema": PANEL_SCHEMA,
    "audit_schema": AUDIT_SCHEMA,
    "source_portfolio_schema": PORTFOLIO_SCHEMA,
    "official_sources": OFFICIAL_SOURCES,
    "minimum_observations": 60,
    "minimum_models": 2,
    "maximum_models": 500,
    "maximum_panel_cells": 250_000,
    "minimum_daily_return": -1.0,
    "maximum_daily_return": 10.0,
    "bootstrap_method": "politis_romano_stationary_bootstrap",
    "bootstrap_replications": 2_000,
    "bootstrap_seed": 260_712,
    "base_expected_block_length_rule": "ceil_sqrt_observation_count",
    "expected_block_length_multipliers": [0.5, 1.0, 2.0],
    "minimum_expected_block_length": 2,
    "maximum_bootstrap_scalar_additions": 100_000_000,
    "variance_estimator": (
        "hansen_2005_stationary_bootstrap_population_long_run_variance"),
    "test_statistic": "studentized_maximum_positive_mean_return_differential",
    "null_recentering": ["lower", "consistent", "upper"],
    "primary_p_value": "consistent",
    "significance_level": 0.05,
    "p_value_comparison": "strict_bootstrap_statistic_greater_than_observed",
    "family_completeness_required": True,
    "exact_daily_alignment_required": True,
    "exact_policy_and_cost_stress_match_required": True,
    "semantic_validation_rebuilds_from_sources": True,
    "safety": POLICY_SAFETY,
}
PANEL_FIELDS = {
    *OUTPUT_SAFETY, "tool", "ok", "schema", "builder_version", "generated_utc",
    "comparison_id", "declared_model_count", "family_complete", "observation_dates",
    "benchmark", "models", "benchmark_returns", "model_returns", "return_semantics",
    "source_contract_compatible", "cost_stress_multiplier", "policy_versions",
    "panel_sha256",
}
SOURCE_FIELDS = {
    "portfolio_id", "source_path", "source_file_sha256", "source_result_sha256",
    "source_daily_path_sha256",
}
AUDIT_FIELDS = {
    *OUTPUT_SAFETY, "tool", "ok", "schema", "audit_version", "generated_utc",
    "policy_path", "policy_sha256", "source_panel_path", "source_panel_file_sha256",
    "source_panel_sha256", "comparison_id", "status", "sample", "hypothesis",
    "method", "analysis", "limitations", "audit_sha256",
}


class SpaError(RuntimeError):
    """Fail-closed SPA contract, input, or inference refusal."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SpaError("non_canonical_value") from exc


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload(document: dict, hash_key: str) -> dict:
    return {key: value for key, value in document.items() if key != hash_key}


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    output = round(float(value), 10)
    return 0.0 if output == 0 else output


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise SpaError("timestamp_naive")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _portable(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def _resolve(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SpaError("path_invalid")
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _read(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file():
        raise SpaError("input_not_regular_file")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise SpaError("input_too_large")
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpaError("input_unreadable_or_malformed") from exc
    if not isinstance(document, dict):
        raise SpaError("input_not_object")
    return document, raw


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict, str]:
    policy, raw = _read(path)
    if policy != EXPECTED_POLICY:
        raise SpaError("spa_policy_contract_invalid")
    return policy, sha256_bytes(raw)


def _portfolio_source(path: Path, policy: dict) -> tuple[dict, dict, bytes]:
    document, raw = _read(path)
    errors = pb.validate_result(document)
    if errors:
        raise SpaError("portfolio_result_invalid:" + ",".join(errors))
    if document.get("schema") != PORTFOLIO_SCHEMA:
        raise SpaError("portfolio_schema_invalid")
    portfolio_id = document.get("variant_id")
    if not isinstance(portfolio_id, str) or not _ID_RE.fullmatch(portfolio_id):
        raise SpaError("portfolio_id_invalid")
    dates: list[str] = []
    returns: list[float] = []
    previous_nav = 1.0
    for index, row in enumerate(document["daily_path"]):
        date = _date(row.get("date"))
        value = row.get("daily_return")
        nav = row.get("normalized_nav")
        if date is None or not _number(value) or not _number(nav) or float(nav) <= 0:
            raise SpaError(f"portfolio_daily_path_semantics_invalid:{index}")
        value = float(value)
        if not (policy["minimum_daily_return"] <= value <= policy["maximum_daily_return"]):
            raise SpaError(f"portfolio_daily_return_out_of_bounds:{index}")
        derived = float(nav) / previous_nav - 1.0
        if abs(value - derived) > 2e-9:
            raise SpaError(f"portfolio_daily_return_nav_mismatch:{index}")
        dates.append(date)
        returns.append(value)
        previous_nav = float(nav)
    source = {
        "portfolio_id": portfolio_id,
        "source_path": _portable(path),
        "source_file_sha256": sha256_bytes(raw),
        "source_result_sha256": document["result_sha256"],
        "source_daily_path_sha256": document["daily_path_sha256"],
    }
    return document, {"source": source, "dates": dates, "returns": returns}, raw


def build_panel(*, comparison_id: str, benchmark_path: Path, model_paths: list[Path],
                declared_model_count: int, generated: Any = None,
                policy_path: Path = DEFAULT_POLICY) -> dict:
    policy, _ = load_policy(policy_path)
    if not isinstance(comparison_id, str) or not _ID_RE.fullmatch(comparison_id):
        raise SpaError("comparison_id_invalid")
    if not isinstance(declared_model_count, int) or isinstance(declared_model_count, bool) \
            or declared_model_count < policy["minimum_models"]:
        raise SpaError("declared_model_count_invalid")
    if not isinstance(model_paths, list) or not (
            policy["minimum_models"] <= len(model_paths) <= policy["maximum_models"]):
        raise SpaError("model_path_count_invalid")
    benchmark_document, benchmark, _ = _portfolio_source(benchmark_path, policy)
    model_rows = [_portfolio_source(path, policy)[:2] for path in model_paths]
    model_documents = [row[0] for row in model_rows]
    models = [row[1] for row in model_rows]
    model_ids = [row["source"]["portfolio_id"] for row in models]
    if len(set(model_ids)) != len(model_ids) or benchmark["source"]["portfolio_id"] in model_ids:
        raise SpaError("portfolio_ids_duplicate_or_benchmark_reused")
    order = sorted(range(len(models)), key=lambda index: model_ids[index])
    models = [models[index] for index in order]
    model_documents = [model_documents[index] for index in order]
    model_ids = [model_ids[index] for index in order]
    dates = benchmark["dates"]
    if any(row["dates"] != dates for row in models):
        raise SpaError("portfolio_daily_dates_not_exactly_aligned")
    if len(dates) * len(models) > policy["maximum_panel_cells"]:
        raise SpaError("panel_cell_limit_exceeded")
    cost = benchmark_document.get("cost_stress_multiplier")
    benchmark_versions = benchmark_document.get("policy_versions")
    if (not isinstance(benchmark_versions, dict)
            or any(not isinstance(benchmark_versions.get(key), str)
                   or not benchmark_versions[key]
                   for key in ("cost", "portfolio", "plan"))):
        raise SpaError("portfolio_policy_versions_invalid")
    for document in model_documents:
        if document.get("cost_stress_multiplier") != cost:
            raise SpaError("portfolio_cost_stress_mismatch")
        versions = document.get("policy_versions")
        if (not isinstance(versions, dict)
                or versions.get("cost") != benchmark_versions["cost"]
                or versions.get("portfolio") != benchmark_versions["portfolio"]
                or not isinstance(versions.get("plan"), str)
                or not versions["plan"]):
            raise SpaError("portfolio_policy_versions_mismatch")
    if not _number(cost) or float(cost) < 1:
        raise SpaError("portfolio_compatibility_fields_invalid")
    versions = {
        "shared_cost": benchmark_versions["cost"],
        "shared_portfolio": benchmark_versions["portfolio"],
        "benchmark_plan": benchmark_versions["plan"],
        "model_plans": {
            document["variant_id"]: document["policy_versions"]["plan"]
            for document in model_documents
        },
    }
    generated_at = dt.datetime.now(UTC) if generated is None else (
        generated.astimezone(UTC) if isinstance(generated, dt.datetime)
        and generated.tzinfo is not None else _parse_utc(generated))
    if generated_at is None:
        raise SpaError("generated_utc_invalid")
    panel = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": PANEL_SCHEMA,
        "builder_version": VERSION,
        "generated_utc": _utc(generated_at),
        "comparison_id": comparison_id,
        "declared_model_count": declared_model_count,
        "family_complete": declared_model_count == len(models),
        "observation_dates": dates,
        "benchmark": benchmark["source"],
        "models": [row["source"] for row in models],
        "benchmark_returns": benchmark["returns"],
        "model_returns": [row["returns"] for row in models],
        "return_semantics": "daily_normalized_capital_net_of_modeled_costs_not_executed",
        "source_contract_compatible": True,
        "cost_stress_multiplier": float(cost),
        "policy_versions": versions,
    }
    panel["panel_sha256"] = sha256_obj(panel)
    errors = validate_panel(panel, rebuild=False)
    if errors:
        raise SpaError("panel_self_validation_failed:" + ",".join(errors))
    return panel


def validate_panel(document: Any, *, rebuild: bool = True) -> list[str]:
    if not isinstance(document, dict):
        return ["panel_not_object"]
    errors: list[str] = []
    if set(document) != PANEL_FIELDS:
        errors.append("panel_fields_invalid")
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"panel_safety_invalid:{key}")
    if document.get("tool") != TOOL or document.get("ok") is not True \
            or document.get("schema") != PANEL_SCHEMA \
            or document.get("builder_version") != VERSION:
        errors.append("panel_identity_invalid")
    if _parse_utc(document.get("generated_utc")) is None:
        errors.append("panel_generated_utc_invalid")
    if not isinstance(document.get("comparison_id"), str) or not _ID_RE.fullmatch(
            document.get("comparison_id", "")):
        errors.append("panel_comparison_id_invalid")
    dates = document.get("observation_dates")
    if not isinstance(dates, list) or not dates or any(_date(value) is None for value in dates) \
            or dates != sorted(set(dates)):
        errors.append("panel_observation_dates_invalid")
        dates = []
    benchmark = document.get("benchmark")
    if not isinstance(benchmark, dict) or set(benchmark) != SOURCE_FIELDS:
        errors.append("panel_benchmark_invalid")
    models = document.get("models")
    if not isinstance(models, list) or len(models) < 2:
        errors.append("panel_models_invalid")
        models = []
    else:
        identifiers = []
        for index, source in enumerate(models):
            if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
                errors.append(f"panel_model_source_invalid:{index}")
                continue
            identifiers.append(source.get("portfolio_id"))
        if identifiers != sorted(identifiers) or len(set(identifiers)) != len(identifiers):
            errors.append("panel_model_order_or_identity_invalid")
        if isinstance(benchmark, dict) and benchmark.get("portfolio_id") in identifiers:
            errors.append("panel_benchmark_reused_as_model")
    for role, source in [("benchmark", benchmark), *[(f"model:{index}", source)
                                                       for index, source in enumerate(models)]]:
        if not isinstance(source, dict):
            continue
        if not isinstance(source.get("portfolio_id"), str) or not _ID_RE.fullmatch(
                source.get("portfolio_id", "")):
            errors.append(f"panel_source_id_invalid:{role}")
        if any(not isinstance(source.get(key), str) or not _SHA_RE.fullmatch(
                source.get(key, "")) for key in (
                    "source_file_sha256", "source_result_sha256", "source_daily_path_sha256")):
            errors.append(f"panel_source_hash_invalid:{role}")
    benchmark_returns = document.get("benchmark_returns")
    model_returns = document.get("model_returns")
    if not isinstance(benchmark_returns, list) or len(benchmark_returns) != len(dates) \
            or any(not _number(value) for value in benchmark_returns):
        errors.append("panel_benchmark_returns_invalid")
    if not isinstance(model_returns, list) or len(model_returns) != len(models) \
            or any(not isinstance(row, list) or len(row) != len(dates)
                   or any(not _number(value) for value in row) for row in model_returns):
        errors.append("panel_model_returns_invalid")
    declared = document.get("declared_model_count")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < len(models):
        errors.append("panel_declared_model_count_invalid")
    if document.get("family_complete") is not (declared == len(models)):
        errors.append("panel_family_complete_invalid")
    if document.get("return_semantics") != (
            "daily_normalized_capital_net_of_modeled_costs_not_executed") \
            or document.get("source_contract_compatible") is not True:
        errors.append("panel_semantics_invalid")
    versions = document.get("policy_versions")
    model_ids = [source.get("portfolio_id") for source in models
                 if isinstance(source, dict)]
    if not _number(document.get("cost_stress_multiplier")) \
            or float(document.get("cost_stress_multiplier", 0)) < 1 \
            or not isinstance(versions, dict) \
            or set(versions) != {
                "shared_cost", "shared_portfolio", "benchmark_plan", "model_plans"} \
            or any(not isinstance(versions.get(key), str) or not versions[key]
                   for key in ("shared_cost", "shared_portfolio", "benchmark_plan")) \
            or not isinstance(versions.get("model_plans"), dict) \
            or set(versions.get("model_plans", {})) != set(model_ids) \
            or any(not isinstance(value, str) or not value
                   for value in versions.get("model_plans", {}).values()):
        errors.append("panel_compatibility_invalid")
    if document.get("panel_sha256") != sha256_obj(_payload(document, "panel_sha256")):
        errors.append("panel_sha256_invalid")
    if errors or not rebuild:
        return sorted(set(errors))
    try:
        expected = build_panel(
            comparison_id=document["comparison_id"],
            benchmark_path=_resolve(document["benchmark"]["source_path"]),
            model_paths=[_resolve(source["source_path"]) for source in document["models"]],
            declared_model_count=document["declared_model_count"],
            generated=document["generated_utc"],
        )
    except (KeyError, SpaError) as exc:
        return [f"panel_rebuild_failed:{exc}"]
    if canonical_bytes(expected) != canonical_bytes(document):
        errors.append("panel_semantic_rebuild_mismatch")
    return sorted(set(errors))


def _stationary_long_run_variance(values: list[float], block_length: int) -> float:
    n = len(values)
    mean = statistics.fmean(values)
    demeaned = [value - mean for value in values]
    variance = sum(value * value for value in demeaned) / n
    continuation = 1.0 - 1.0 / block_length
    for lag in range(1, n):
        covariance = sum(demeaned[index] * demeaned[index + lag]
                         for index in range(n - lag)) / n
        weight = ((1.0 - lag / n) * continuation ** lag
                  + (lag / n) * continuation ** (n - lag))
        variance += 2.0 * weight * covariance
    return variance


def _block_lengths(n: int, policy: dict) -> list[int]:
    base = math.ceil(math.sqrt(n))
    minimum = policy["minimum_expected_block_length"]
    return sorted({max(minimum, min(n, math.floor(base * multiplier + 0.5)))
                   for multiplier in policy["expected_block_length_multipliers"]})


def _spa_configuration(differentials: list[list[float]], model_ids: list[str],
                       block_length: int, policy: dict) -> dict:
    n = len(differentials)
    model_count = len(model_ids)
    columns = [[row[index] for row in differentials] for index in range(model_count)]
    means = [statistics.fmean(column) for column in columns]
    variances = [_stationary_long_run_variance(column, block_length) for column in columns]
    degenerate = [model_ids[index] for index, value in enumerate(variances)
                  if not math.isfinite(value) or value <= _EPS]
    if degenerate:
        return {
            "available": False,
            "reason": "nonpositive_stationary_bootstrap_long_run_variance",
            "expected_block_length": block_length,
            "restart_probability": _round(1.0 / block_length),
            "degenerate_model_ids": degenerate,
        }
    roots = [math.sqrt(value) for value in variances]
    root_n = math.sqrt(n)
    observed_components = [root_n * means[index] / roots[index]
                           for index in range(model_count)]
    observed = max(0.0, max(observed_components))
    threshold_scale = 2.0 * math.log(math.log(n))
    centers = {
        "lower": [max(0.0, value) for value in means],
        "consistent": [
            value if value >= -math.sqrt((variances[index] / n) * threshold_scale) else 0.0
            for index, value in enumerate(means)
        ],
        "upper": list(means),
    }
    relevant = sum(center != 0.0 or means[index] >= 0.0
                   for index, center in enumerate(centers["consistent"]))
    exceedances = {name: 0 for name in centers}
    reps = policy["bootstrap_replications"]
    rng = random.Random(policy["bootstrap_seed"])
    restart_probability = 1.0 / block_length
    for _ in range(reps):
        candidates = [rng.randrange(n) for _index in range(n)]
        uniforms = [rng.random() for _index in range(n)]
        indices = [candidates[0]]
        for position in range(1, n):
            indices.append(candidates[position] if uniforms[position] < restart_probability
                           else (indices[-1] + 1) % n)
        sums = [0.0] * model_count
        for row_index in indices:
            row = differentials[row_index]
            for model_index, value in enumerate(row):
                sums[model_index] += value
        bootstrap_means = [value / n for value in sums]
        for name, recenter in centers.items():
            statistic = max(0.0, max(
                root_n * (bootstrap_means[index] - recenter[index]) / roots[index]
                for index in range(model_count)))
            if statistic > observed:
                exceedances[name] += 1
    p_values = {name: _round(exceedances[name] / reps) for name in centers}
    if not (p_values["lower"] <= p_values["consistent"] <= p_values["upper"]):
        raise SpaError("spa_p_value_order_invariant_failed")
    best_mean = max(means)
    best_index = min((index for index, value in enumerate(means) if value == best_mean),
                     key=lambda index: model_ids[index])
    alpha = policy["significance_level"]
    return {
        "available": True,
        "reason": None,
        "expected_block_length": block_length,
        "restart_probability": _round(restart_probability),
        "observed_test_statistic": _round(observed),
        "best_sample_model_id": model_ids[best_index],
        "best_sample_mean_return_differential": _round(means[best_index]),
        "consistent_relevant_model_count": relevant,
        "p_values": p_values,
        "strict_exceedance_counts": exceedances,
        "bootstrap_replications": reps,
        "minimum_p_value_resolution": _round(1.0 / reps),
        "monte_carlo_standard_errors": {
            name: _round(math.sqrt(value * (1.0 - value) / reps))
            for name, value in p_values.items()
        },
        "primary_null_rejected_at_configured_alpha": p_values["consistent"] <= alpha,
        "zero_exceedance_is_not_zero_true_probability": any(
            count == 0 for count in exceedances.values()),
        "models": [
            {
                "model_id": model_ids[index],
                "mean_return_differential": _round(means[index]),
                "stationary_bootstrap_long_run_variance": _round(variances[index]),
                "studentized_mean_component": _round(observed_components[index]),
                "consistent_null_recenter": _round(centers["consistent"][index]),
            }
            for index in range(model_count)
        ],
    }


def _analysis(panel: dict, policy: dict) -> tuple[str, dict]:
    n = len(panel["observation_dates"])
    model_count = len(panel["models"])
    if panel["family_complete"] is not True:
        return "incomplete_family", {
            "available": False,
            "reason": "declared_model_family_incomplete",
            "configurations": [],
            "robust_across_configured_block_lengths": False,
        }
    if n < policy["minimum_observations"]:
        return "insufficient_sample", {
            "available": False,
            "reason": "observation_count_below_policy_minimum",
            "configurations": [],
            "robust_across_configured_block_lengths": False,
        }
    lengths = _block_lengths(n, policy)
    operations = n * model_count * policy["bootstrap_replications"] * len(lengths)
    if operations > policy["maximum_bootstrap_scalar_additions"]:
        return "computational_limit", {
            "available": False,
            "reason": "bootstrap_operation_limit_exceeded",
            "estimated_scalar_additions": operations,
            "configurations": [],
            "robust_across_configured_block_lengths": False,
        }
    benchmark = [float(value) for value in panel["benchmark_returns"]]
    model_returns = [[float(value) for value in row] for row in panel["model_returns"]]
    differentials = [[model_returns[model_index][date_index] - benchmark[date_index]
                      for model_index in range(model_count)] for date_index in range(n)]
    model_ids = [source["portfolio_id"] for source in panel["models"]]
    configurations = [_spa_configuration(differentials, model_ids, length, policy)
                      for length in lengths]
    available = all(row["available"] for row in configurations)
    if not available:
        return "degenerate", {
            "available": False,
            "reason": "one_or_more_block_length_configurations_unavailable",
            "estimated_scalar_additions": operations,
            "configurations": configurations,
            "robust_across_configured_block_lengths": False,
        }
    robust = all(row["primary_null_rejected_at_configured_alpha"]
                 for row in configurations)
    return "reviewable", {
        "available": True,
        "reason": None,
        "estimated_scalar_additions": operations,
        "configurations": configurations,
        "primary_expected_block_length": math.ceil(math.sqrt(n)),
        "primary_configuration": next(
            index for index, row in enumerate(configurations)
            if row["expected_block_length"] == math.ceil(math.sqrt(n))),
        "robust_across_configured_block_lengths": robust,
        "statistical_evidence_only": True,
        "establishes_profitability": False,
        "grants_strategy_promotion": False,
    }


def build_audit(*, panel_path: Path, generated: Any = None,
                policy_path: Path = DEFAULT_POLICY) -> dict:
    policy, policy_sha = load_policy(policy_path)
    panel, panel_raw = _read(panel_path)
    errors = validate_panel(panel)
    if errors:
        raise SpaError("source_panel_invalid:" + ",".join(errors))
    generated_at = dt.datetime.now(UTC) if generated is None else (
        generated.astimezone(UTC) if isinstance(generated, dt.datetime)
        and generated.tzinfo is not None else _parse_utc(generated))
    panel_time = _parse_utc(panel["generated_utc"])
    if generated_at is None or panel_time is None or generated_at < panel_time:
        raise SpaError("audit_generated_before_panel")
    status, analysis = _analysis(panel, policy)
    audit = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": AUDIT_SCHEMA,
        "audit_version": VERSION,
        "generated_utc": _utc(generated_at),
        "policy_path": _portable(policy_path),
        "policy_sha256": policy_sha,
        "source_panel_path": _portable(panel_path),
        "source_panel_file_sha256": sha256_bytes(panel_raw),
        "source_panel_sha256": panel["panel_sha256"],
        "comparison_id": panel["comparison_id"],
        "status": status,
        "sample": {
            "observation_count": len(panel["observation_dates"]),
            "model_count": len(panel["models"]),
            "declared_model_count": panel["declared_model_count"],
            "family_complete": panel["family_complete"],
            "minimum_observations": policy["minimum_observations"],
            "date_start": panel["observation_dates"][0],
            "date_end": panel["observation_dates"][-1],
            "benchmark_id": panel["benchmark"]["portfolio_id"],
            "cost_stress_multiplier": panel["cost_stress_multiplier"],
        },
        "hypothesis": {
            "null": "no_tested_model_has_positive_expected_return_differential_vs_benchmark",
            "alternative": "at_least_one_tested_model_has_positive_expected_return_differential",
            "direction": "model_daily_return_minus_benchmark_daily_return",
            "significance_level": policy["significance_level"],
        },
        "method": {
            "test": "hansen_2005_superior_predictive_ability",
            "test_statistic": policy["test_statistic"],
            "bootstrap": policy["bootstrap_method"],
            "variance_estimator": policy["variance_estimator"],
            "null_recentering": policy["null_recentering"],
            "primary_p_value": policy["primary_p_value"],
            "p_value_comparison": policy["p_value_comparison"],
            "bootstrap_seed": policy["bootstrap_seed"],
            "same_uniform_draws_across_block_length_sensitivity": True,
        },
        "analysis": analysis,
        "limitations": sorted({
            "a_rejected_null_does_not_identify_causality_capacity_or_future_profitability",
            "stationarity_and_weak_dependence_are_assumptions_not_proven_by_this_test",
            "the_declared_model_family_cannot_reveal_undisclosed_experiments",
            "modeled_daily_returns_are_not_observed_human_fills",
            "block_length_choice_is_material_so_all_configured_sensitivities_are_reported",
            "zero_bootstrap_exceedances_mean_below_monte_carlo_resolution_not_zero_probability",
            "no_strategy_buy_risk_freeze_metric_broker_or_execution_authority",
        }),
    }
    audit["audit_sha256"] = sha256_obj(audit)
    final_errors = validate_audit(audit, rebuild=False)
    if final_errors:
        raise SpaError("audit_self_validation_failed:" + ",".join(final_errors))
    return audit


def validate_audit(document: Any, *, rebuild: bool = True) -> list[str]:
    if not isinstance(document, dict):
        return ["audit_not_object"]
    errors: list[str] = []
    if set(document) != AUDIT_FIELDS:
        errors.append("audit_fields_invalid")
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"audit_safety_invalid:{key}")
    if document.get("tool") != TOOL or document.get("ok") is not True \
            or document.get("schema") != AUDIT_SCHEMA \
            or document.get("audit_version") != VERSION:
        errors.append("audit_identity_invalid")
    if _parse_utc(document.get("generated_utc")) is None:
        errors.append("audit_generated_utc_invalid")
    if document.get("status") not in {
            "reviewable", "incomplete_family", "insufficient_sample", "computational_limit",
            "degenerate"}:
        errors.append("audit_status_invalid")
    for key in ("policy_sha256", "source_panel_file_sha256", "source_panel_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(document.get(key, "")):
            errors.append(f"audit_hash_invalid:{key}")
    if not isinstance(document.get("sample"), dict) or not isinstance(
            document.get("hypothesis"), dict) or not isinstance(document.get("method"), dict):
        errors.append("audit_contract_sections_invalid")
    analysis = document.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("available") not in {True, False} \
            or not isinstance(analysis.get("configurations"), list) \
            or analysis.get("robust_across_configured_block_lengths") not in {True, False}:
        errors.append("audit_analysis_invalid")
    elif analysis.get("available") is True:
        if document.get("status") != "reviewable" or analysis.get(
                "establishes_profitability") is not False or analysis.get(
                    "grants_strategy_promotion") is not False \
                or analysis.get("statistical_evidence_only") is not True:
            errors.append("audit_analysis_authority_invalid")
        for index, configuration in enumerate(analysis["configurations"]):
            if not isinstance(configuration, dict) or configuration.get("available") is not True:
                errors.append(f"audit_configuration_invalid:{index}")
                continue
            p_values = configuration.get("p_values")
            if not isinstance(p_values, dict) or set(p_values) != {"lower", "consistent", "upper"} \
                    or any(not _number(value) or not 0 <= float(value) <= 1
                           for value in p_values.values()) \
                    or not (p_values["lower"] <= p_values["consistent"] <= p_values["upper"]):
                errors.append(f"audit_configuration_p_values_invalid:{index}")
    if document.get("audit_sha256") != sha256_obj(_payload(document, "audit_sha256")):
        errors.append("audit_sha256_invalid")
    if errors or not rebuild:
        return sorted(set(errors))
    try:
        expected = build_audit(
            panel_path=_resolve(document["source_panel_path"]),
            generated=document["generated_utc"],
            policy_path=_resolve(document["policy_path"]),
        )
    except (KeyError, SpaError) as exc:
        return [f"audit_rebuild_failed:{exc}"]
    if canonical_bytes(expected) != canonical_bytes(document):
        errors.append("audit_semantic_rebuild_mismatch")
    return sorted(set(errors))


def check_contracts(policy_path: Path = DEFAULT_POLICY) -> dict:
    policy, policy_sha = load_policy(policy_path)
    schema_hashes = {}
    for path, expected_id in (
            (POLICY_SCHEMA_PATH, "https://charles.local/schemas/quant_spa_policy.schema.json"),
            (PANEL_SCHEMA_PATH, "https://charles.local/schemas/quant_spa_panel.schema.json"),
            (AUDIT_SCHEMA_PATH, "https://charles.local/schemas/quant_spa_audit.schema.json")):
        schema, raw = _read(path)
        if schema.get("$id") != expected_id:
            raise SpaError("schema_id_invalid")
        schema_hashes[_portable(path)] = sha256_bytes(raw)
    return {
        "ok": True,
        "version": VERSION,
        "policy_sha256": policy_sha,
        "bootstrap_replications": policy["bootstrap_replications"],
        "schema_sha256": schema_hashes,
        "safety": OUTPUT_SAFETY,
    }


def _write_once(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=True,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise SpaError("output_already_exists") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    panel = sub.add_parser("build-panel")
    panel.add_argument("--comparison-id", required=True)
    panel.add_argument("--benchmark", type=Path, required=True)
    panel.add_argument("--model", type=Path, action="append", required=True)
    panel.add_argument("--declared-models", type=int, required=True)
    panel.add_argument("--out", type=Path, required=True)
    panel.add_argument("--now")
    panel.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    audit = sub.add_parser("audit")
    audit.add_argument("--panel", type=Path, required=True)
    audit.add_argument("--out", type=Path, required=True)
    audit.add_argument("--now")
    audit.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    validate_panel_command = sub.add_parser("validate-panel")
    validate_panel_command.add_argument("--input", type=Path, required=True)
    validate_audit_command = sub.add_parser("validate-audit")
    validate_audit_command.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            result = check_contracts(args.policy)
        elif args.command == "build-panel":
            document = build_panel(
                comparison_id=args.comparison_id, benchmark_path=args.benchmark,
                model_paths=args.model, declared_model_count=args.declared_models,
                generated=args.now, policy_path=args.policy)
            _write_once(args.out, document)
            result = {"ok": True, "written": str(args.out),
                      "panel_sha256": document["panel_sha256"], "safety": OUTPUT_SAFETY}
        elif args.command == "audit":
            document = build_audit(
                panel_path=args.panel, generated=args.now, policy_path=args.policy)
            _write_once(args.out, document)
            result = {"ok": True, "written": str(args.out), "status": document["status"],
                      "audit_sha256": document["audit_sha256"], "safety": OUTPUT_SAFETY}
        else:
            document, _ = _read(args.input)
            errors = (validate_panel(document) if args.command == "validate-panel"
                      else validate_audit(document))
            result = {"ok": not errors, "errors": errors, "safety": OUTPUT_SAFETY}
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result.get("ok") else 1
    except (SpaError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "safety": OUTPUT_SAFETY},
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
