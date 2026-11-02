"""Probability of backtest overfitting (PBO) by combinatorially symmetric cross-validation.

CSCV splits the T x N matrix of per-period returns (T periods, N strategy
configurations) into S equal blocks of consecutive rows. For every way of
choosing S/2 blocks as the in-sample set (the rest is out-of-sample), it picks
the strategy with the best in-sample Sharpe ratio and records its relative
out-of-sample rank w = rank / (N + 1), where rank 1 is the worst. The logit
lambda = ln(w / (1 - w)) is negative when the in-sample winner lands below the
out-of-sample median. PBO is the share of splits with lambda <= 0. Every split
and its complement are both used, so in-sample and out-of-sample sets have the
same size and the procedure is symmetric.

With no skill in any strategy the in-sample winner's out-of-sample rank is
uniform, so PBO is about 0.5. A strategy whose edge persists keeps winning out
of sample, which drives PBO toward 0.

Reference:
    Bailey, D. H., Borwein, J., Lopez de Prado, M. and Zhu, Q. J. (2017). The
    probability of backtest overfitting. Journal of Computational Finance
    20(4), 39-69. https://doi.org/10.21314/JCF.2016.322
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math
from typing import Sequence


@dataclass(frozen=True)
class OverfittingReport:
    """CSCV results for one strategy matrix."""

    pbo: float
    splits: int
    logits: tuple[float, ...]
    probability_of_loss: float
    degradation_slope: float
    in_sample_sharpes: tuple[float, ...]
    out_of_sample_sharpes: tuple[float, ...]


def _sharpe(total: float, squares: float, count: int) -> float:
    """Per-period Sharpe ratio from a sum, a sum of squares and a count."""
    mean = total / count
    spread = (squares - total * total / count) / (count - 1)
    return mean / math.sqrt(spread) if spread > 0 else 0.0


def _ascending_rank(values: Sequence[float], index: int) -> float:
    """Rank of values[index] from 1 (lowest) to len(values); ties share their average rank."""
    target = values[index]
    lower = sum(value < target for value in values)
    equal = sum(value == target for value in values)
    return lower + (equal + 1) / 2


def probability_of_backtest_overfitting(matrix: Sequence[Sequence[float]],
                                        blocks: int = 16) -> OverfittingReport:
    """Estimate PBO for a T x N matrix of per-period strategy returns.

    Args:
        matrix: ``matrix[t][n]`` is the return of strategy ``n`` in period ``t``.
        blocks: The number S of equal row blocks; even, at least 2, and a
            divisor of T. The number of splits is C(S, S/2).

    The in-sample winner is the lowest-numbered strategy among ties. The
    degradation slope is the least-squares slope of the winner's out-of-sample
    Sharpe ratio on its in-sample Sharpe ratio across splits.
    """
    periods = len(matrix)
    strategies = len(matrix[0]) if periods else 0
    if strategies < 2 or any(len(row) != strategies for row in matrix):
        raise ValueError("need a rectangular matrix with at least 2 strategies")
    if blocks < 2 or blocks % 2 or periods % blocks:
        raise ValueError("blocks must be even, at least 2 and divide the number of periods")
    size = periods // blocks
    if size < 2:
        raise ValueError("each block needs at least 2 periods")
    sums = [[0.0] * strategies for _ in range(blocks)]
    squares = [[0.0] * strategies for _ in range(blocks)]
    for block in range(blocks):
        rows = matrix[block * size:(block + 1) * size]
        for strategy in range(strategies):
            column = [float(row[strategy]) for row in rows]
            sums[block][strategy] = math.fsum(column)
            squares[block][strategy] = math.fsum(value * value for value in column)
    total = [math.fsum(sums[b][s] for b in range(blocks)) for s in range(strategies)]
    total_squares = [math.fsum(squares[b][s] for b in range(blocks)) for s in range(strategies)]
    half = blocks // 2 * size
    logits, chosen_in, chosen_out = [], [], []
    for chosen in combinations(range(blocks), blocks // 2):
        in_sum = [math.fsum(sums[b][s] for b in chosen) for s in range(strategies)]
        in_squares = [math.fsum(squares[b][s] for b in chosen) for s in range(strategies)]
        in_sharpe = [_sharpe(in_sum[s], in_squares[s], half) for s in range(strategies)]
        out_sharpe = [_sharpe(total[s] - in_sum[s], total_squares[s] - in_squares[s], half)
                      for s in range(strategies)]
        winner = max(range(strategies), key=in_sharpe.__getitem__)
        relative = _ascending_rank(out_sharpe, winner) / (strategies + 1)
        logits.append(math.log(relative / (1 - relative)))
        chosen_in.append(in_sharpe[winner])
        chosen_out.append(out_sharpe[winner])
    splits = len(logits)
    mean_in, mean_out = math.fsum(chosen_in) / splits, math.fsum(chosen_out) / splits
    spread = math.fsum((x - mean_in) ** 2 for x in chosen_in)
    slope = (math.fsum((x - mean_in) * (y - mean_out) for x, y in zip(chosen_in, chosen_out)) / spread
             if spread > 0 else 0.0)
    return OverfittingReport(
        pbo=sum(value <= 0 for value in logits) / splits,
        splits=splits,
        logits=tuple(logits),
        probability_of_loss=sum(value < 0 for value in chosen_out) / splits,
        degradation_slope=slope,
        in_sample_sharpes=tuple(chosen_in),
        out_of_sample_sharpes=tuple(chosen_out),
    )
