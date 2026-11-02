#!/usr/bin/env python3
"""Synthetic backtest-overfitting demo: the best of many no-skill strategies looks good and is flagged.

Fictional data only. Nothing here uses market data, recommends a strategy or
claims that any strategy is profitable. Exit status 1 means the diagnostics did
not reach the expected verdicts.
"""
from __future__ import annotations

import argparse
import math
import sys

from backtest_diagnostics.calibration import fixed_strategy_psr_rate, pass_rates
from backtest_diagnostics.cscv import probability_of_backtest_overfitting
from backtest_diagnostics.reality_check import data_snooping_tests
from backtest_diagnostics.sharpe import select_best
from backtest_diagnostics.synthetic import PERIODS_PER_YEAR, add_skill, columns, no_skill_matrix

PERIODS, STRATEGIES, BLOCKS, RESAMPLES, MEAN_BLOCK, SEED = 1260, 100, 10, 500, 10.0, 1
SKILL_SHARPE = 3.0  # annualized edge given to strategy 0 in the second scenario
DSR_MIN, PBO_MAX, ALPHA = 0.95, 0.10, 0.05


def diagnose(matrix: list[list[float]]) -> dict:
    """Every diagnostic for one strategy matrix; returns differentials are against a zero benchmark."""
    selection = select_best(columns(matrix))
    overfitting = probability_of_backtest_overfitting(matrix, blocks=BLOCKS)
    reality, spa = data_snooping_tests(matrix, resamples=RESAMPLES, mean_block=MEAN_BLOCK, seed=SEED)
    verdicts = {"DSR": selection.dsr >= DSR_MIN, "PBO": overfitting.pbo <= PBO_MAX,
                "Reality Check": reality.p_value < ALPHA, "SPA": spa.p_value < ALPHA}
    return {"selection": selection, "pbo": overfitting, "rc": reality, "spa": spa, "passes": verdicts}


def report(name: str, result: dict) -> list[str]:
    selection, overfitting, passes = result["selection"], result["pbo"], result["passes"]
    annual = selection.moments.sharpe * math.sqrt(PERIODS_PER_YEAR)
    hurdle = selection.expected_maximum_sharpe * math.sqrt(PERIODS_PER_YEAR)

    def row(label: str, value: float, verdict: str = "", note: str = "") -> str:
        flag = ("pass" if passes[verdict] else "FLAG") if verdict else ""
        return f"  {label:<34}{value:>7.4f}  {flag:<5}{note}".rstrip()

    return [
        f"{name}: best strategy {selection.selected} of {selection.trials}, "
        f"annualized Sharpe {annual:.2f} over {selection.moments.periods} periods",
        row("PSR vs 0, ignoring the trials", selection.psr),
        row(f"DSR after {selection.trials} trials", selection.dsr, "DSR",
            f"expected best no-skill Sharpe {hurdle:.2f} annualized"),
        row(f"PBO, CSCV with {BLOCKS} blocks", overfitting.pbo, "PBO",
            f"{overfitting.splits} splits, out-of-sample loss in {overfitting.probability_of_loss:.0%}"),
        row("Reality Check p-value", result["rc"].p_value, "Reality Check"),
        row("SPA p-value", result["spa"].p_value, "SPA"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--null", type=int, metavar="RUNS",
                        help="instead of the demo, measure pass rates over RUNS no-skill universes")
    parser.add_argument("--power", type=int, metavar="RUNS",
                        help="instead of the demo, measure pass rates with skill in strategy 0")
    parser.add_argument("--psr", type=int, metavar="STRATEGIES",
                        help="instead of the demo, check PSR on STRATEGIES no-skill strategies judged one at a time")
    parser.add_argument("--block", type=float, default=MEAN_BLOCK,
                        help=f"mean stationary-bootstrap block length for --null/--power (default {MEAN_BLOCK:g})")
    parser.add_argument("--sharpe", type=float, default=3.0,
                        help="annualized edge of strategy 0 for --power (default 3.0)")
    args = parser.parse_args(argv)
    if args.psr:
        rate = fixed_strategy_psr_rate(args.psr)
        print(f"{args.psr} no-skill strategies of 500 periods, each judged alone: PSR >= 0.95 in {rate:.4f} "
              f"(standard error {(0.05 * 0.95 / args.psr) ** 0.5:.4f} around 0.05)")
        return 0
    if args.null or args.power:
        runs = args.null or args.power
        edge = 0.0 if args.null else args.sharpe
        rates = pass_rates(runs, annual_sharpe=edge, mean_block=args.block)
        print(f"{runs} synthetic universes of 20 strategies x 500 periods, edge {edge} in strategy 0, "
              f"mean block {args.block:g}; pass rate (standard error):")
        for name, rate in rates.items():
            error = "" if name == "mean PBO" else f" ({(rate * (1 - rate) / runs) ** 0.5:.3f})"
            print(f"  {name:<26}{rate:.3f}{error}")
        return 0
    noise = no_skill_matrix(PERIODS, STRATEGIES, seed=SEED)
    scenarios = {"No skill": diagnose(noise), "Skill in strategy 0": diagnose(add_skill(noise, 0, SKILL_SHARPE))}
    print(f"Synthetic Student-t returns (5 degrees of freedom), {STRATEGIES} strategies, {PERIODS} periods; "
          f"pass means DSR >= {DSR_MIN}, PBO <= {PBO_MAX} and p < {ALPHA}.\n")
    for name, result in scenarios.items():
        print("\n".join(report(name, result)) + "\n")
    expected = (not any(scenarios["No skill"]["passes"].values())
                and all(scenarios["Skill in strategy 0"]["passes"].values())
                and scenarios["Skill in strategy 0"]["selection"].selected == 0)
    print("Every diagnostic flags the no-skill winner and passes the injected skill."
          if expected else "Unexpected verdicts: see the rows above.")
    return 0 if expected else 1


if __name__ == "__main__":
    sys.exit(main())
