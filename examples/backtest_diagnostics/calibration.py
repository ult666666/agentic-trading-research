"""How often each diagnostic passes a strategy, over many seeded synthetic universes.

Under the null every strategy is pure noise, so every pass is a false positive.
With injected skill in strategy 0, passes measure power. The single-strategy
PSR row checks the PSR itself: with one strategy fixed in advance there is no
selection, so it should pass about 5% of no-skill runs.
"""
from __future__ import annotations

from .cscv import probability_of_backtest_overfitting
from .reality_check import data_snooping_tests
from .sharpe import moments, probabilistic_sharpe_ratio, select_best
from .synthetic import add_skill, columns, no_skill_matrix

DSR_MIN, PBO_MAX, ALPHA = 0.95, 0.10, 0.05


def pass_rates(runs: int, periods: int = 500, strategies: int = 20, blocks: int = 10,
               resamples: int = 199, mean_block: float = 10.0, annual_sharpe: float = 0.0,
               seed: int = 1000) -> dict[str, float]:
    """Share of runs in which each diagnostic passes; also the mean PBO."""
    counts = dict.fromkeys(["PSR, one fixed strategy", "PSR, best of N", "DSR, best of N",
                            "PBO <= 0.10", "Reality Check p < 0.05", "SPA p < 0.05"], 0)
    pbo_total = 0.0
    for run in range(runs):
        matrix = no_skill_matrix(periods, strategies, seed=seed + run)
        if annual_sharpe:
            matrix = add_skill(matrix, 0, annual_sharpe)
        series = columns(matrix)
        fixed = moments(series[0])
        selection = select_best(series)
        overfitting = probability_of_backtest_overfitting(matrix, blocks=blocks)
        reality, spa = data_snooping_tests(matrix, resamples=resamples, mean_block=mean_block, seed=run)
        counts["PSR, one fixed strategy"] += probabilistic_sharpe_ratio(
            fixed.sharpe, 0.0, fixed.periods, fixed.skewness, fixed.kurtosis) >= DSR_MIN
        counts["PSR, best of N"] += selection.psr >= DSR_MIN
        counts["DSR, best of N"] += selection.dsr >= DSR_MIN
        counts["PBO <= 0.10"] += overfitting.pbo <= PBO_MAX
        counts["Reality Check p < 0.05"] += reality.p_value < ALPHA
        counts["SPA p < 0.05"] += spa.p_value < ALPHA
        pbo_total += overfitting.pbo
    rates = {name: count / runs for name, count in counts.items()}
    rates["mean PBO"] = pbo_total / runs
    return rates


def fixed_strategy_psr_rate(strategies: int, periods: int = 500, seed: int = 41) -> float:
    """Share of independent no-skill strategies, each judged alone, with PSR >= 0.95 (about 0.05)."""
    rates = [moments(series) for series in columns(no_skill_matrix(periods, strategies, seed=seed))]
    return sum(probabilistic_sharpe_ratio(m.sharpe, 0.0, m.periods, m.skewness, m.kurtosis) >= DSR_MIN
               for m in rates) / strategies
