"""Sharpe-ratio significance after non-normal returns and many trials.

The Probabilistic Sharpe Ratio (PSR) is the probability that a strategy's true
Sharpe ratio exceeds a benchmark, given an estimate from T returns with sample
skewness and kurtosis. The Deflated Sharpe Ratio (DSR) is the PSR against the
Sharpe ratio that the best of N no-skill trials would be expected to reach, so it
corrects for selecting the best backtest out of many.

Sharpe ratios here are per period (not annualized): mean / sample standard
deviation. Skewness is m3 / m2^1.5 and kurtosis is m4 / m2^2 (3 for a normal
distribution), with central moments mk computed with the 1/T denominator.

References:
    Bailey, D. H. and Lopez de Prado, M. (2012). The Sharpe ratio efficient
    frontier. Journal of Risk 15(2), 3-44. https://doi.org/10.21314/JOR.2012.255

    Bailey, D. H. and Lopez de Prado, M. (2014). The deflated Sharpe ratio:
    correcting for selection bias, backtest overfitting and non-normality.
    Journal of Portfolio Management 40(5), 94-107.
    https://doi.org/10.3905/jpm.2014.40.5.094
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import NormalDist, fmean, variance
from typing import Sequence

EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()


@dataclass(frozen=True)
class Moments:
    """Summary of one return series."""

    periods: int
    mean: float
    stdev: float
    sharpe: float
    skewness: float
    kurtosis: float


def moments(returns: Sequence[float]) -> Moments:
    """Mean, sample standard deviation, per-period Sharpe ratio, skewness and kurtosis."""
    values = [float(value) for value in returns]
    count = len(values)
    if count < 3:
        raise ValueError("at least 3 returns are needed")
    mean = fmean(values)
    deviations = [value - mean for value in values]
    m2 = math.fsum(d * d for d in deviations) / count
    if m2 == 0:
        raise ValueError("returns have no variance")
    m3 = math.fsum(d ** 3 for d in deviations) / count
    m4 = math.fsum(d ** 4 for d in deviations) / count
    stdev = math.sqrt(m2 * count / (count - 1))
    return Moments(count, mean, stdev, mean / stdev, m3 / m2 ** 1.5, m4 / m2 ** 2)


def probabilistic_sharpe_ratio(sharpe: float, benchmark: float, periods: int,
                               skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """PSR: the probability that the true Sharpe ratio exceeds ``benchmark``.

    PSR = Phi((SR - SR*) * sqrt(T - 1) / sqrt(1 - g3 * SR + (g4 - 1) / 4 * SR^2)),
    from the asymptotic variance of the Sharpe-ratio estimator under non-normal
    returns (Bailey and Lopez de Prado 2012).
    """
    if periods < 2:
        raise ValueError("at least 2 periods are needed")
    spread = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    if spread <= 0:
        raise ValueError("these moments give a nonpositive variance for the Sharpe ratio")
    return _NORMAL.cdf((sharpe - benchmark) * math.sqrt(periods - 1) / math.sqrt(spread))


def expected_maximum_sharpe(trials: int, trial_variance: float, mean: float = 0.0) -> float:
    """Expected largest Sharpe ratio among ``trials`` independent no-skill trials.

    E[max SR] = mean + sqrt(V) * ((1 - gamma) * Z^-1(1 - 1/N) + gamma * Z^-1(1 - 1/(N e))),
    where V is the variance of the trials' Sharpe ratios and gamma is the
    Euler-Mascheroni constant (Bailey and Lopez de Prado 2014). It is an
    approximation to the expected maximum of N normal draws.
    """
    if trials < 2:
        raise ValueError("at least 2 trials are needed")
    if trial_variance < 0:
        raise ValueError("trial variance cannot be negative")
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return mean + math.sqrt(trial_variance) * ((1 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second)


def deflated_sharpe_ratio(sharpe: float, periods: int, trials: int, trial_variance: float,
                          skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """DSR: the PSR against the expected maximum Sharpe ratio of ``trials`` no-skill trials."""
    hurdle = expected_maximum_sharpe(trials, trial_variance)
    return probabilistic_sharpe_ratio(sharpe, hurdle, periods, skewness, kurtosis)


def minimum_track_record_length(sharpe: float, benchmark: float, skewness: float = 0.0,
                                kurtosis: float = 3.0, confidence: float = 0.95) -> float:
    """Returns needed before the PSR against ``benchmark`` reaches ``confidence``.

    MinTRL = 1 + (1 - g3 * SR + (g4 - 1) / 4 * SR^2) * (Z^-1(confidence) / (SR - SR*))^2
    (Bailey and Lopez de Prado 2012). Infinite when SR does not exceed SR*.
    """
    if sharpe <= benchmark:
        return math.inf
    spread = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    return 1.0 + spread * (_NORMAL.inv_cdf(confidence) / (sharpe - benchmark)) ** 2


@dataclass(frozen=True)
class SelectionReport:
    """The best of several backtests, judged with and without the number of trials."""

    selected: int
    trials: int
    moments: Moments
    trial_sharpe_variance: float
    psr: float
    expected_maximum_sharpe: float
    dsr: float
    minimum_track_record_length: float


def select_best(columns: Sequence[Sequence[float]], benchmark: float = 0.0,
                confidence: float = 0.95) -> SelectionReport:
    """Pick the column with the highest Sharpe ratio and compute its PSR and DSR.

    ``columns`` holds one return series per trial, all of the same length. The
    PSR ignores how many trials were run; the DSR does not.
    """
    if len(columns) < 2:
        raise ValueError("at least 2 trials are needed")
    summaries = [moments(column) for column in columns]
    if len({summary.periods for summary in summaries}) != 1:
        raise ValueError("every trial needs the same number of returns")
    sharpes = [summary.sharpe for summary in summaries]
    selected = max(range(len(sharpes)), key=sharpes.__getitem__)
    best = summaries[selected]
    spread = variance(sharpes)
    return SelectionReport(
        selected=selected,
        trials=len(columns),
        moments=best,
        trial_sharpe_variance=spread,
        psr=probabilistic_sharpe_ratio(best.sharpe, benchmark, best.periods, best.skewness, best.kurtosis),
        expected_maximum_sharpe=expected_maximum_sharpe(len(columns), spread),
        dsr=deflated_sharpe_ratio(best.sharpe, best.periods, len(columns), spread, best.skewness, best.kurtosis),
        minimum_track_record_length=minimum_track_record_length(
            best.sharpe, benchmark, best.skewness, best.kurtosis, confidence),
    )
