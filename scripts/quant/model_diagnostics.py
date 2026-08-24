#!/usr/bin/env python3
"""Statistical diagnostics for conservative quant backtest results.

Metrics are per hypothetical trade and deliberately not annualized: irregular, overlapping
swing-trade observations do not justify a ``sqrt(252)`` conversion.  Small samples remain
exploratory.  Multiple testing is surfaced through a Bonferroni threshold from the experiment
manifest's full trial count; DSR and CSCV/PBO are explicitly unavailable until their data
requirements are met rather than approximated from an unsuitable single return vector.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import backtest as qb  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.model_diagnostics"
SCHEMA = "quant_model_diagnostics/1.0"
MIN_REVIEWABLE = 30
ALPHA = 0.05
_NUM = (int, float)


def _base() -> dict:
    return {**qf.SAFETY_FIELDS, "tool": TOOL}


def _refuse(error: str, **extra) -> dict:
    return {**_base(), "ok": False, "error": error, **extra}


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256_obj(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _payload(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "diagnostics_sha256"}


def _round(value) -> float | None:
    if value is None:
        return None
    out = round(float(value), 10)
    return 0.0 if out == 0 else out


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return max_drawdown


def _wilson_interval(wins: int, total: int, z: float = 1.9599639845):
    if total == 0:
        return None
    p = wins / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / denominator
    return [_round(max(0.0, center - margin)), _round(min(1.0, center + margin))]


def analyze(result: dict) -> dict:
    validation_errors = qb.validate_result(result)
    if validation_errors:
        return _refuse("invalid_backtest_result", errors=validation_errors)
    completed = sorted(
        (record for record in result["records"] if record["simulation_status"] == "completed"),
        key=lambda record: (record["event_date"], record["scenario_id"]),
    )
    returns = [float(record["net_return_after_modeled_costs"]) for record in completed]
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    n = len(returns)
    average = _mean(returns)
    sample_stdev = statistics.stdev(returns) if n >= 2 else None
    sharpe = (average / sample_stdev if sample_stdev and sample_stdev > 0 else None)
    downside = math.sqrt(statistics.fmean(min(0.0, value) ** 2 for value in returns)) if returns else None
    sortino = (average / downside if downside and downside > 0 else None)
    gross_profit, gross_loss = sum(winners), abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    mean_ci = None
    if n >= 2 and sample_stdev is not None:
        margin = 1.9599639845 * sample_stdev / math.sqrt(n)
        mean_ci = [_round(average - margin), _round(average + margin)]
    n_trials = result["n_trials"]
    bonferroni_alpha = ALPHA / n_trials
    warnings = []
    if n < MIN_REVIEWABLE:
        warnings.append(f"small_sample_n_lt_{MIN_REVIEWABLE}")
    if result["summary"]["same_bar_ambiguity_count"]:
        warnings.append("contains_conservative_same_bar_stop_first_records")
    if result["summary"]["gap_through_stop_count"]:
        warnings.append("contains_modeled_gap_through_stop_records")
    if n == 0:
        warnings.append("no_completed_hypothetical_paths")

    diagnostics = {
        **_base(),
        "ok": True,
        "schema": SCHEMA,
        "source_result_sha256": result["result_sha256"],
        "experiment_id": result["experiment_id"],
        "n_trials": n_trials,
        "sample": {
            "completed": n,
            "no_fill": result["summary"]["no_fill_count"],
            "excluded": result["summary"]["excluded_count"],
            "pending": result["summary"]["pending_count"],
            "quality": "reviewable" if n >= MIN_REVIEWABLE else "exploratory",
            "minimum_reviewable": MIN_REVIEWABLE,
            "warnings": warnings,
        },
        "metrics": {
            "average_net_return": _round(average),
            "median_net_return": _round(statistics.median(returns)) if returns else None,
            "sample_stdev_net_return": _round(sample_stdev),
            "sharpe_per_trade_not_annualized": _round(sharpe),
            "sortino_per_trade_not_annualized": _round(sortino),
            "annualized_sharpe": None,
            "annualized_sharpe_reason": "irregular_overlapping_trade_observations",
            "max_compounded_drawdown": _round(_max_drawdown(returns)),
            "hit_rate": _round(len(winners) / n) if n else None,
            "average_winner": _round(_mean(winners)),
            "average_loser": _round(_mean(losers)),
            "expectancy": _round(average),
            "profit_factor": _round(profit_factor),
            "profit_factor_reason": None if profit_factor is not None else "no_negative_returns",
            "average_hold_sessions": _round(_mean([
                float(record["duration_sessions"]) for record in completed
            ])),
            "average_mfe_pct": _round(_mean([float(record["mfe_pct"]) for record in completed])),
            "average_mae_pct": _round(_mean([float(record["mae_pct"]) for record in completed])),
        },
        "confidence_intervals": {
            "mean_net_return_95_normal_approx": mean_ci,
            "mean_interval_reason": None if mean_ci is not None else "requires_at_least_2_returns",
            "hit_rate_95_wilson": _wilson_interval(len(winners), n),
            "small_sample_caveat": n < MIN_REVIEWABLE,
        },
        "multiple_testing": {
            "familywise_alpha": ALPHA,
            "bonferroni_alpha_per_trial": _round(bonferroni_alpha),
            "bonferroni_confidence_required": _round(1.0 - bonferroni_alpha),
            "selection_significance_assessed": False,
            "selection_significance_reason": "diagnostics_do_not_supply_valid_independent_p_values",
            "deflated_sharpe_ratio": None,
            "deflated_sharpe_reason": (
                "requires_reviewable_sample_and_distributional_inputs_not_established_here"
            ),
            "probability_of_backtest_overfitting": None,
            "pbo_reason": "requires_CSCV_return_matrix_across_multiple_strategy_variants",
        },
        "ordered_completed_scenarios": [record["scenario_id"] for record in completed],
        "interpretation": (
            "Retrospective hypothetical-path diagnostics only. No metric demonstrates live "
            "performance or authorizes a production, scoring, risk, or execution change."
        ),
    }
    diagnostics["diagnostics_sha256"] = _sha256_obj(_payload(diagnostics))
    errors = validate_diagnostics(diagnostics)
    if errors:
        return _refuse("internal_diagnostics_validation_failed", errors=errors)
    return diagnostics


def validate_diagnostics(doc) -> list[str]:
    if not isinstance(doc, dict):
        return ["diagnostics_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in qf.SAFETY_FIELDS.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True:
        errors.append("diagnostics_ok_must_be_true")
    if doc.get("schema") != SCHEMA:
        errors.append("diagnostics_schema_mismatch")
    if not isinstance(doc.get("source_result_sha256"), str) or not qb._SHA_RE.fullmatch(
            doc.get("source_result_sha256", "")):
        errors.append("source_result_sha256_invalid")
    sample = doc.get("sample")
    if not isinstance(sample, dict) or not isinstance(sample.get("completed"), int):
        errors.append("sample_missing_or_invalid")
    metrics = doc.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics_missing")
    multiple = doc.get("multiple_testing")
    if not isinstance(multiple, dict):
        errors.append("multiple_testing_missing")
    else:
        if multiple.get("selection_significance_assessed") is not False:
            errors.append("selection_significance_must_remain_unassessed")
        if multiple.get("probability_of_backtest_overfitting") is not None:
            errors.append("pbo_claimed_without_CSCV_matrix")
    if doc.get("diagnostics_sha256") != _sha256_obj(_payload(doc)):
        errors.append("diagnostics_sha256_mismatch")
    return errors


def _read(path: Path):
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
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_quant_diagnostics_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
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


def _print(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backtest model diagnostics; diagnostic only.")
    sub = parser.add_subparsers(dest="command", required=True)
    analyze_cmd = sub.add_parser("analyze")
    analyze_cmd.add_argument("--backtest", required=True)
    analyze_cmd.add_argument("--out", default="-")
    analyze_cmd.add_argument("--replace", action="store_true")
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    path = Path(args.backtest if args.command == "analyze" else args.path)
    doc, problem = _read(path)
    if problem:
        _print(_refuse("input_read_failed", detail=problem, path=str(path)))
        return 1
    if args.command == "validate":
        errors = validate_diagnostics(doc)
        _print({**_base(), "ok": not errors, "errors": errors, "path": str(path),
                "diagnostics_sha256": doc.get("diagnostics_sha256")})
        return 0 if not errors else 1
    result = analyze(doc)
    if result.get("ok") is not True:
        _print(result)
        return 1
    if args.out == "-":
        _print(result)
        return 0
    out = Path(args.out)
    try:
        write_problem = _atomic_write(out, result, args.replace)
    except OSError as exc:
        _print(_refuse("output_write_failed", detail=type(exc).__name__, path=str(out)))
        return 1
    if write_problem:
        _print(_refuse(write_problem, path=str(out)))
        return 1
    _print({**_base(), "ok": True, "written": str(out),
            "diagnostics_sha256": result["diagnostics_sha256"], "sample": result["sample"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
