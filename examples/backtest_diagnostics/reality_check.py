"""White's Reality Check and Hansen's SPA test, using the stationary bootstrap.

Both tests ask whether the best of N strategies beats a benchmark once the
search over all N is taken into account. The input is a T x N matrix of
differentials: ``d[t][k]`` is the return of strategy k minus the benchmark's
return in period t. The null hypothesis is that no strategy has a positive
expected differential.

Reality Check (White 2000): the statistic is V = max_k sqrt(T) * mean_k. Its
null distribution is approximated by V*_b = max_k sqrt(T) * (mean*_kb - mean_k)
over bootstrap resamples b, which puts every strategy exactly at the boundary
of the null (expected differential zero).

SPA, consistent version (Hansen 2005): each strategy is divided by omega_k, the
bootstrap standard deviation of sqrt(T) * mean*_k, so a volatile strategy cannot
dominate the maximum. T_SPA = max(0, max_k sqrt(T) * mean_k / omega_k). In the
resamples, a strategy whose t-ratio is below -sqrt(2 log log T) keeps its own
negative mean instead of being moved to zero, so clearly bad strategies do not
inflate the p-value. The Reality Check is conservative when such strategies are
present; SPA is not.

Stationary bootstrap (Politis and Romano 1994): a resample joins blocks that
start at uniformly random rows, with geometric lengths of mean ``mean_block``,
wrapping from the last row to the first. It keeps the serial dependence inside
blocks and is itself stationary. All strategies share each resample's rows, so
their correlation is kept too.

Both p-values are (1 + #{b : resampled statistic >= observed}) / (1 + B).

References:
    White, H. (2000). A reality check for data snooping. Econometrica 68(5),
    1097-1126. https://doi.org/10.1111/1468-0262.00152

    Hansen, P. R. (2005). A test for superior predictive ability. Journal of
    Business and Economic Statistics 23(4), 365-380.
    https://doi.org/10.1198/073500105000000063

    Politis, D. N. and Romano, J. P. (1994). The stationary bootstrap. Journal
    of the American Statistical Association 89(428), 1303-1313.
    https://doi.org/10.1080/01621459.1994.10476870
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Sequence


@dataclass(frozen=True)
class SnoopingTest:
    """One data-snooping test result."""

    name: str
    statistic: float
    p_value: float
    best: int
    resamples: int
    mean_block: float


def stationary_blocks(periods: int, mean_block: float, rng: random.Random) -> list[tuple[int, int]]:
    """(start, length) blocks of one stationary-bootstrap resample; lengths sum to ``periods``.

    Starts are uniform over the rows; lengths are geometric with mean
    ``mean_block`` (drawn by inversion), and the last block is cut to fit.
    """
    if periods < 1 or not 1 <= mean_block < math.inf:
        raise ValueError("periods must be at least 1 and mean_block finite and at least 1")
    stop = 1.0 / mean_block
    blocks, filled = [], 0
    while filled < periods:
        start = rng.randrange(periods)
        # log1p keeps log(1 - stop) nonzero when stop is tiny (mean blocks above about 1e16).
        length = 1 if stop >= 1 else 1 + int(math.log(1.0 - rng.random()) / math.log1p(-stop))
        length = min(length, periods - filled)
        blocks.append((start, length))
        filled += length
    return blocks


def resample_rows(periods: int, blocks: Sequence[tuple[int, int]]) -> list[int]:
    """The row indices a list of blocks selects, wrapping from the last row to the first."""
    return [(start + offset) % periods for start, length in blocks for offset in range(length)]


def _segments(periods: int, blocks: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Blocks as half-open row ranges that do not wrap."""
    pieces = []
    for start, length in blocks:
        end = start + length
        if end <= periods:
            pieces.append((start, end))
        else:
            pieces.append((start, periods))
            pieces.append((0, end - periods))
    return pieces


def bootstrap_means(differentials: Sequence[Sequence[float]], resamples: int, mean_block: float,
                    seed: int = 0) -> list[list[float]]:
    """Mean differential of every strategy in each stationary-bootstrap resample (B x N).

    Sums over each block come from per-strategy prefix sums, so a resample costs
    O(N x blocks) instead of O(N x T).
    """
    periods = len(differentials)
    strategies = len(differentials[0]) if periods else 0
    if periods < 3 or strategies < 1 or any(len(row) != strategies for row in differentials):
        raise ValueError("need a rectangular matrix with at least 3 periods")
    if resamples < 1:
        raise ValueError("at least 1 resample is needed")
    prefixes = []
    for strategy in range(strategies):
        running, prefix = 0.0, [0.0]
        for row in differentials:
            running += float(row[strategy])
            prefix.append(running)
        prefixes.append(prefix)
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        pieces = _segments(periods, stationary_blocks(periods, mean_block, rng))
        means.append([math.fsum(prefix[end] - prefix[start] for start, end in pieces) / periods
                      for prefix in prefixes])
    return means


def data_snooping_tests(differentials: Sequence[Sequence[float]], resamples: int = 1000,
                        mean_block: float = 10.0, seed: int = 0) -> tuple[SnoopingTest, SnoopingTest]:
    """Run White's Reality Check and Hansen's consistent SPA test on the same resamples.

    Returns (reality_check, spa). ``best`` is the strategy with the largest
    mean differential for the Reality Check and the largest t-ratio for SPA.
    """
    periods = len(differentials)
    strategies = len(differentials[0]) if periods else 0
    boot = bootstrap_means(differentials, resamples, mean_block, seed)
    means = [math.fsum(float(row[k]) for row in differentials) / periods for k in range(strategies)]
    root = math.sqrt(periods)
    observed = max(root * mean for mean in means)
    best = max(range(strategies), key=means.__getitem__)
    exceed = sum(max(root * (sample[k] - means[k]) for k in range(strategies)) >= observed
                 for sample in boot)
    reality = SnoopingTest("Reality Check", observed, (1 + exceed) / (1 + resamples), best,
                           resamples, mean_block)

    omegas = [math.sqrt(math.fsum((root * (sample[k] - means[k])) ** 2 for sample in boot) / resamples)
              for k in range(strategies)]
    usable = [k for k in range(strategies) if omegas[k] > 0]
    ratios = {k: root * means[k] / omegas[k] for k in usable}
    statistic = max([0.0, *ratios.values()])
    threshold = -math.sqrt(2 * math.log(math.log(periods)))
    centre = {k: means[k] if ratios[k] >= threshold else 0.0 for k in usable}
    spa_exceed = sum(
        max([0.0, *(root * (sample[k] - centre[k]) / omegas[k] for k in usable)]) >= statistic
        for sample in boot)
    spa_best = max(usable, key=ratios.__getitem__) if usable else best
    spa = SnoopingTest("SPA", statistic, (1 + spa_exceed) / (1 + resamples), spa_best,
                       resamples, mean_block)
    return reality, spa
