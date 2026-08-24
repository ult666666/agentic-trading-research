#!/usr/bin/env python3
"""Selection-bias diagnostics for aligned quant-strategy return matrices.

This module implements two research-only diagnostics:

* Deflated Sharpe Ratio (DSR), using the Bailey/Lopez de Prado adjustment for
  multiple trials and non-normal returns.
* Combinatorially Symmetric Cross-Validation (CSCV), reporting the Probability
  of Backtest Overfitting (PBO).

The input is an aligned observation-by-variant matrix.  The matrix can be built
from validated ``quant_backtest_result/1.0`` files; completed paths contribute
their modeled net return and a settled no-fill contributes zero.  Pending or
excluded paths make the matrix invalid rather than silently changing the sample.

All outputs are offline diagnostics.  They never feed production, never activate
a strategy, never authorize a Buy, and never represent executed performance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path
from statistics import NormalDist
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import backtest as qb  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.selection_bias"
MATRIX_SCHEMA = "quant_selection_matrix/1.0"
RESULT_SCHEMA = "quant_selection_bias/1.0"
MIN_OBSERVATIONS = 30
MIN_VARIANTS = 2
MIN_SLICES = 4
MAX_COMBINATIONS = 5000
EULER_MASCHERONI = 0.5772156649015329
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)


def _base() -> dict:
    return {**qf.SAFETY_FIELDS, "tool": TOOL}


def _refuse(error: str, **extra) -> dict:
    return {**_base(), "ok": False, "error": error, **extra}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _matrix_payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "matrix_sha256"}


def _result_payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "selection_bias_sha256"}


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    out = round(float(value), 10)
    return 0.0 if out == 0 else out


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


def _sharpe(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    stdev = statistics.stdev(values)
    if stdev <= 0:
        # A variant that never enters has a settled all-zero return vector. Treat it as
        # zero performance so a strict entry gate cannot invalidate the entire trial family.
        # Any constant nonzero vector remains undefined/suspicious and fails closed.
        return 0.0 if all(value == 0.0 for value in values) else None
    return statistics.fmean(values) / stdev


def _moments(values: list[float]) -> tuple[float | None, float | None]:
    """Population skewness and Pearson kurtosis for the DSR denominator."""
    if len(values) < 3:
        return None, None
    mean = statistics.fmean(values)
    second = statistics.fmean((value - mean) ** 2 for value in values)
    if second <= 0:
        return None, None
    third = statistics.fmean((value - mean) ** 3 for value in values)
    fourth = statistics.fmean((value - mean) ** 4 for value in values)
    return third / second ** 1.5, fourth / second ** 2


def build_matrix(experiment_id: str, observation_ids: list[str], variant_ids: list[str],
                 returns: list[list[float]], selected_variant_id: str,
                 n_trials_declared: int, source_result_sha256s: dict[str, str]) -> dict:
    doc = {
        **_base(),
        "ok": True,
        "schema": MATRIX_SCHEMA,
        "experiment_id": experiment_id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "observation_ids": observation_ids,
        "variant_ids": variant_ids,
        "returns": returns,
        "selected_variant_id": selected_variant_id,
        "n_trials_declared": n_trials_declared,
        "source_result_sha256s": source_result_sha256s,
        "settled_no_fill_return": 0.0,
        "matrix_orientation": "rows_are_observations_columns_are_variants",
        "trial_coverage_complete": n_trials_declared == len(variant_ids),
    }
    doc["matrix_sha256"] = _sha256_obj(_matrix_payload(doc))
    errors = validate_matrix(doc)
    if errors:
        return _refuse("matrix_self_validation_failed", errors=errors)
    return doc


def build_matrix_from_results(experiment_id: str, variant_results: dict[str, dict],
                              selected_variant_id: str, n_trials_declared: int) -> dict:
    if not isinstance(variant_results, dict) or len(variant_results) < MIN_VARIANTS:
        return _refuse("insufficient_variant_results",
                       minimum_variants=MIN_VARIANTS)
    variant_ids = sorted(variant_results)
    if any(not isinstance(value, str) or not _ID_RE.fullmatch(value) for value in variant_ids):
        return _refuse("variant_id_invalid")
    by_variant: dict[str, dict[str, float]] = {}
    source_hashes: dict[str, str] = {}
    for variant_id in variant_ids:
        result = variant_results[variant_id]
        errors = qb.validate_result(result)
        if errors:
            return _refuse("invalid_backtest_result", variant_id=variant_id, errors=errors)
        if result.get("variant_id") != variant_id:
            return _refuse("variant_id_binding_mismatch", variant_id=variant_id,
                           result_variant_id=result.get("variant_id"))
        rows: dict[str, float] = {}
        for record in result["records"]:
            status = record["simulation_status"]
            observation_id = f"{record['event_date']}:{record['event_id']}"
            if observation_id in rows:
                return _refuse("duplicate_observation_in_variant",
                               variant_id=variant_id, observation_id=observation_id)
            if status == "completed":
                value = record.get("net_return_after_modeled_costs")
                if not _number(value):
                    return _refuse("completed_return_invalid", variant_id=variant_id,
                                   observation_id=observation_id)
                rows[observation_id] = float(value)
            elif status == "no_fill":
                rows[observation_id] = 0.0
            else:
                return _refuse("unsettled_or_excluded_observation",
                               variant_id=variant_id, observation_id=observation_id,
                               simulation_status=status)
        if not rows:
            return _refuse("variant_has_no_observations", variant_id=variant_id)
        by_variant[variant_id] = rows
        source_hashes[variant_id] = result["result_sha256"]

    reference = set(by_variant[variant_ids[0]])
    for variant_id in variant_ids[1:]:
        if set(by_variant[variant_id]) != reference:
            return _refuse("variant_observation_sets_not_aligned", variant_id=variant_id,
                           missing=sorted(reference - set(by_variant[variant_id])),
                           extra=sorted(set(by_variant[variant_id]) - reference))
    observation_ids = sorted(reference)
    rows = [[by_variant[variant_id][observation_id] for variant_id in variant_ids]
            for observation_id in observation_ids]
    return build_matrix(experiment_id, observation_ids, variant_ids, rows,
                        selected_variant_id, n_trials_declared, source_hashes)


def validate_matrix(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["matrix_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in qf.SAFETY_FIELDS.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True:
        errors.append("matrix_ok_must_be_true")
    if doc.get("schema") != MATRIX_SCHEMA:
        errors.append("matrix_schema_mismatch")
    if not isinstance(doc.get("experiment_id"), str) or not _ID_RE.fullmatch(
            doc.get("experiment_id", "")):
        errors.append("experiment_id_invalid")
    if _parse_utc(doc.get("created_utc")) is None:
        errors.append("created_utc_invalid")
    observation_ids = doc.get("observation_ids")
    variant_ids = doc.get("variant_ids")
    if (not isinstance(observation_ids, list) or not observation_ids
            or any(not isinstance(value, str) or not value for value in observation_ids)
            or len(set(observation_ids)) != len(observation_ids)):
        errors.append("observation_ids_invalid")
    if (not isinstance(variant_ids, list) or len(variant_ids) < MIN_VARIANTS
            or any(not isinstance(value, str) or not _ID_RE.fullmatch(value)
                   for value in variant_ids)
            or len(set(variant_ids)) != len(variant_ids)):
        errors.append("variant_ids_invalid")
    matrix = doc.get("returns")
    if not isinstance(matrix, list) or not isinstance(observation_ids, list) or (
            len(matrix) != len(observation_ids)):
        errors.append("return_matrix_row_count_invalid")
    elif isinstance(variant_ids, list):
        for index, row in enumerate(matrix):
            if (not isinstance(row, list) or len(row) != len(variant_ids)
                    or any(not _number(value) for value in row)):
                errors.append(f"return_matrix_row_invalid:{index}")
    selected = doc.get("selected_variant_id")
    if not isinstance(variant_ids, list) or selected not in variant_ids:
        errors.append("selected_variant_id_invalid")
    n_trials = doc.get("n_trials_declared")
    if (not isinstance(n_trials, int) or isinstance(n_trials, bool)
            or not isinstance(variant_ids, list) or n_trials < len(variant_ids)):
        errors.append("n_trials_declared_invalid")
    if isinstance(n_trials, int) and isinstance(variant_ids, list):
        if doc.get("trial_coverage_complete") is not (n_trials == len(variant_ids)):
            errors.append("trial_coverage_complete_incorrect")
    source_hashes = doc.get("source_result_sha256s")
    if not isinstance(source_hashes, dict) or not isinstance(variant_ids, list) or (
            set(source_hashes) != set(variant_ids)):
        errors.append("source_result_sha256s_invalid")
    elif any(not isinstance(value, str) or not _SHA_RE.fullmatch(value)
             for value in source_hashes.values()):
        errors.append("source_result_sha256_invalid")
    if doc.get("settled_no_fill_return") != 0.0:
        errors.append("settled_no_fill_return_must_be_zero")
    if doc.get("matrix_orientation") != "rows_are_observations_columns_are_variants":
        errors.append("matrix_orientation_invalid")
    if doc.get("matrix_sha256") != _sha256_obj(_matrix_payload(doc)):
        errors.append("matrix_sha256_mismatch")
    return errors


def _deflated_sharpe(matrix: dict, sharpes: list[float]) -> dict:
    if matrix["trial_coverage_complete"] is not True:
        return {"available": False, "reason": "incomplete_trial_matrix"}
    selected_index = matrix["variant_ids"].index(matrix["selected_variant_id"])
    selected_returns = [float(row[selected_index]) for row in matrix["returns"]]
    selected_sharpe = sharpes[selected_index]
    if len(sharpes) < 2:
        return {"available": False, "reason": "requires_multiple_variants"}
    variance = statistics.variance(sharpes)
    if variance <= 0:
        return {"available": False, "reason": "variant_sharpe_variance_zero"}
    skewness, kurtosis = _moments(selected_returns)
    if skewness is None or kurtosis is None:
        return {"available": False, "reason": "selected_return_moments_unavailable"}
    n_trials = matrix["n_trials_declared"]
    normal = NormalDist()
    z_first = normal.inv_cdf(1.0 - 1.0 / n_trials)
    z_second = normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    expected_max_sharpe = math.sqrt(variance) * (
        (1.0 - EULER_MASCHERONI) * z_first + EULER_MASCHERONI * z_second)
    denominator_term = (1.0 - skewness * selected_sharpe
                        + ((kurtosis - 1.0) / 4.0) * selected_sharpe ** 2)
    if denominator_term <= 0:
        return {"available": False, "reason": "nonpositive_dsr_denominator"}
    z_score = ((selected_sharpe - expected_max_sharpe)
               * math.sqrt(len(selected_returns) - 1) / math.sqrt(denominator_term))
    return {
        "available": True,
        "reason": None,
        "selected_variant_id": matrix["selected_variant_id"],
        "selected_sharpe_per_observation_not_annualized": _round(selected_sharpe),
        "expected_maximum_sharpe_hurdle": _round(expected_max_sharpe),
        "variant_sharpe_variance": _round(variance),
        "selected_return_skewness": _round(skewness),
        "selected_return_pearson_kurtosis": _round(kurtosis),
        "z_score": _round(z_score),
        "probability_selected_sharpe_exceeds_multiple_trial_hurdle": _round(
            normal.cdf(z_score)),
        "annualized": False,
    }


def _rank_ascending(values: list[float], selected_index: int) -> float:
    selected = values[selected_index]
    less = sum(value < selected for value in values)
    equal = sum(value == selected for value in values)
    return 1.0 + less + (equal - 1.0) / 2.0


def _cscv_pbo(matrix: dict, slices: int, max_combinations: int) -> dict:
    n_observations = len(matrix["observation_ids"])
    n_variants = len(matrix["variant_ids"])
    if matrix["trial_coverage_complete"] is not True:
        return {"available": False, "reason": "incomplete_trial_matrix"}
    if (not isinstance(slices, int) or isinstance(slices, bool) or slices < MIN_SLICES
            or slices % 2 != 0):
        return {"available": False, "reason": "slices_must_be_even_and_at_least_four"}
    if n_observations % slices != 0:
        return {"available": False, "reason": "observations_not_divisible_by_slices",
                "observation_count": n_observations, "slices": slices}
    block_size = n_observations // slices
    if block_size < 2:
        return {"available": False, "reason": "fewer_than_two_observations_per_slice"}
    combination_count = math.comb(slices, slices // 2)
    if combination_count > max_combinations:
        return {"available": False, "reason": "combination_limit_exceeded",
                "combination_count": combination_count,
                "maximum_combinations": max_combinations}

    blocks = [list(range(index * block_size, (index + 1) * block_size))
              for index in range(slices)]
    paths = []
    selected_counts = {variant_id: 0 for variant_id in matrix["variant_ids"]}
    in_sample_tie_count = 0
    for selected_blocks in itertools.combinations(range(slices), slices // 2):
        selected_set = set(selected_blocks)
        in_indices = [row for block in selected_blocks for row in blocks[block]]
        out_indices = [row for block in range(slices) if block not in selected_set
                       for row in blocks[block]]
        in_sharpes = []
        out_sharpes = []
        for variant in range(n_variants):
            in_value = _sharpe([float(matrix["returns"][row][variant])
                                for row in in_indices])
            out_value = _sharpe([float(matrix["returns"][row][variant])
                                 for row in out_indices])
            if in_value is None or out_value is None:
                return {"available": False,
                        "reason": "zero_variance_or_insufficient_split_returns"}
            in_sharpes.append(in_value)
            out_sharpes.append(out_value)
        best = max(in_sharpes)
        winners = [index for index, value in enumerate(in_sharpes) if value == best]
        if len(winners) != 1:
            in_sample_tie_count += 1
        selected_index = min(winners, key=lambda index: matrix["variant_ids"][index])
        selected_id = matrix["variant_ids"][selected_index]
        selected_counts[selected_id] += 1
        rank = _rank_ascending(out_sharpes, selected_index)
        relative_rank = rank / (n_variants + 1.0)
        logit = math.log(relative_rank / (1.0 - relative_rank))
        paths.append({
            "in_sample_blocks": list(selected_blocks),
            "selected_variant_id": selected_id,
            "in_sample_sharpe": _round(in_sharpes[selected_index]),
            "out_of_sample_sharpe": _round(out_sharpes[selected_index]),
            "out_of_sample_rank_ascending": _round(rank),
            "out_of_sample_relative_rank": _round(relative_rank),
            "rank_logit": _round(logit),
            "overfit_path": logit <= 0.0,
        })
    overfit = sum(path["overfit_path"] for path in paths)
    degradation = [path["out_of_sample_sharpe"] - path["in_sample_sharpe"]
                   for path in paths]
    return {
        "available": True,
        "reason": None,
        "method": "combinatorially_symmetric_cross_validation",
        "performance_statistic": "sharpe_per_observation_not_annualized",
        "slices": slices,
        "block_size": block_size,
        "combination_count": combination_count,
        "probability_of_backtest_overfitting": _round(overfit / len(paths)),
        "overfit_path_count": overfit,
        "in_sample_tie_count": in_sample_tie_count,
        "in_sample_tie_break": "lexicographically_smallest_variant_id",
        "median_sharpe_degradation": _round(statistics.median(degradation)),
        "selected_variant_counts": selected_counts,
        "paths": paths,
    }


def analyze_matrix(matrix: dict, *, slices: int = 8,
                   max_combinations: int = MAX_COMBINATIONS) -> dict:
    errors = validate_matrix(matrix)
    if errors:
        return _refuse("invalid_selection_matrix", errors=errors)
    n_observations = len(matrix["observation_ids"])
    if n_observations < MIN_OBSERVATIONS:
        return _refuse("insufficient_observations", observed=n_observations,
                       minimum=MIN_OBSERVATIONS)
    sharpes: list[float] = []
    for index, variant_id in enumerate(matrix["variant_ids"]):
        value = _sharpe([float(row[index]) for row in matrix["returns"]])
        if value is None:
            return _refuse("variant_sharpe_unavailable", variant_id=variant_id)
        sharpes.append(value)
    selected_index = matrix["variant_ids"].index(matrix["selected_variant_id"])
    selected_returns = [float(row[selected_index]) for row in matrix["returns"]]
    selected_mean = statistics.fmean(selected_returns)
    selected_stdev = statistics.stdev(selected_returns)
    naive_t = selected_mean / (selected_stdev / math.sqrt(n_observations))
    result = {
        **_base(),
        "ok": True,
        "schema": RESULT_SCHEMA,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "experiment_id": matrix["experiment_id"],
        "source_matrix_sha256": matrix["matrix_sha256"],
        "sample": {
            "observation_count": n_observations,
            "variant_count": len(matrix["variant_ids"]),
            "n_trials_declared": matrix["n_trials_declared"],
            "trial_coverage_complete": matrix["trial_coverage_complete"],
            "minimum_observations": MIN_OBSERVATIONS,
        },
        "selected_variant": {
            "variant_id": matrix["selected_variant_id"],
            "mean_net_return": _round(selected_mean),
            "sample_stdev_net_return": _round(selected_stdev),
            "sharpe_per_observation_not_annualized": _round(sharpes[selected_index]),
            "naive_iid_t_statistic": _round(naive_t),
            "naive_iid_t_statistic_authoritative": False,
            "dependence_warning": (
                "Overlapping swing-trade observations can violate independence; the naive "
                "t-statistic is descriptive and is not an activation gate."
            ),
        },
        "variant_sharpes_per_observation_not_annualized": {
            variant_id: _round(sharpes[index])
            for index, variant_id in enumerate(matrix["variant_ids"])
        },
        "zero_return_variant_count": sum(
            all(float(row[index]) == 0.0 for row in matrix["returns"])
            for index in range(len(matrix["variant_ids"]))
        ),
        "zero_return_variant_convention": (
            "A settled all-zero/no-fill variant is assigned Sharpe 0; constant nonzero "
            "variants remain invalid."
        ),
        "deflated_sharpe_ratio": _deflated_sharpe(matrix, sharpes),
        "cscv_pbo": _cscv_pbo(matrix, slices, max_combinations),
        "interpretation": (
            "Selection-bias diagnostic over modeled, non-executed returns. A favorable result "
            "does not demonstrate live profitability or authorize production activation."
        ),
    }
    result["selection_bias_sha256"] = _sha256_obj(_result_payload(result))
    final_errors = validate_result(result)
    if final_errors:
        return _refuse("selection_bias_self_validation_failed", errors=final_errors)
    return result


def validate_result(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["selection_bias_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in qf.SAFETY_FIELDS.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True:
        errors.append("result_ok_must_be_true")
    if doc.get("schema") != RESULT_SCHEMA:
        errors.append("result_schema_mismatch")
    if _parse_utc(doc.get("generated_utc")) is None:
        errors.append("generated_utc_invalid")
    if not isinstance(doc.get("source_matrix_sha256"), str) or not _SHA_RE.fullmatch(
            doc.get("source_matrix_sha256", "")):
        errors.append("source_matrix_sha256_invalid")
    sample = doc.get("sample")
    if not isinstance(sample, dict):
        errors.append("sample_missing")
    else:
        for field in ("observation_count", "variant_count", "n_trials_declared"):
            if not isinstance(sample.get(field), int) or isinstance(sample.get(field), bool):
                errors.append(f"sample_field_invalid:{field}")
        if sample.get("trial_coverage_complete") not in (True, False):
            errors.append("sample_trial_coverage_invalid")
    selected = doc.get("selected_variant")
    if not isinstance(selected, dict) or not isinstance(selected.get("variant_id"), str):
        errors.append("selected_variant_invalid")
    elif selected.get("naive_iid_t_statistic_authoritative") is not False:
        errors.append("naive_t_statistic_must_be_non_authoritative")
    for field in ("deflated_sharpe_ratio", "cscv_pbo"):
        value = doc.get(field)
        if not isinstance(value, dict) or value.get("available") not in (True, False):
            errors.append(f"{field}_invalid")
    if doc.get("selection_bias_sha256") != _sha256_obj(_result_payload(doc)):
        errors.append("selection_bias_sha256_mismatch")
    return errors


def _read(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "file_missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"file_unreadable_or_malformed:{type(exc).__name__}"


def _atomic_write(path: Path, value: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_selection_bias_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return None


def _parse_variant_args(values: list[str]) -> tuple[dict[str, dict] | None, dict | None]:
    results: dict[str, dict] = {}
    for raw in values:
        if "=" not in raw:
            return None, _refuse("variant_argument_requires_id_equals_path", value=raw)
        variant_id, path_raw = raw.split("=", 1)
        if variant_id in results:
            return None, _refuse("duplicate_variant_argument", variant_id=variant_id)
        doc, error = _read(Path(path_raw))
        if error:
            return None, _refuse("variant_file_unreadable", variant_id=variant_id,
                                 reason=error)
        results[variant_id] = doc
    return results, None


def _emit(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DSR and CSCV/PBO diagnostics; offline only.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-matrix")
    build.add_argument("--experiment-id", required=True)
    build.add_argument("--variant", action="append", required=True,
                       help="VARIANT_ID=/path/to/backtest.json (repeatable)")
    build.add_argument("--selected", required=True)
    build.add_argument("--trials", required=True, type=int)
    build.add_argument("--out", required=True)
    build.add_argument("--replace", action="store_true")

    analyze = sub.add_parser("analyze")
    analyze.add_argument("--matrix", required=True)
    analyze.add_argument("--slices", type=int, default=8)
    analyze.add_argument("--max-combinations", type=int, default=MAX_COMBINATIONS)
    analyze.add_argument("--out", required=True)
    analyze.add_argument("--replace", action="store_true")

    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    validate.add_argument("--kind", choices=("matrix", "result"), required=True)

    args = parser.parse_args(argv)
    if args.command == "build-matrix":
        results, error = _parse_variant_args(args.variant)
        if error:
            _emit(error)
            return 1
        output = build_matrix_from_results(args.experiment_id, results or {}, args.selected,
                                           args.trials)
        if output.get("ok") is not True:
            _emit(output)
            return 1
        write_error = _atomic_write(Path(args.out), output, args.replace)
        if write_error:
            _emit(_refuse(write_error, output_path=args.out))
            return 1
        _emit({**_base(), "ok": True, "written": args.out,
               "matrix_sha256": output["matrix_sha256"]})
        return 0

    doc, error = _read(Path(args.path if args.command == "validate" else args.matrix))
    if error:
        _emit(_refuse(error))
        return 1
    if args.command == "validate":
        errors = validate_matrix(doc) if args.kind == "matrix" else validate_result(doc)
        _emit({**_base(), "ok": not errors, "errors": errors, "path": args.path})
        return 0 if not errors else 1

    output = analyze_matrix(doc, slices=args.slices,
                            max_combinations=args.max_combinations)
    if output.get("ok") is not True:
        _emit(output)
        return 1
    write_error = _atomic_write(Path(args.out), output, args.replace)
    if write_error:
        _emit(_refuse(write_error, output_path=args.out))
        return 1
    _emit({**_base(), "ok": True, "written": args.out,
           "selection_bias_sha256": output["selection_bias_sha256"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
