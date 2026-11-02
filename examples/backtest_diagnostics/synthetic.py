"""Synthetic strategy returns for the overfitting demo. No market data is used or implied."""
from __future__ import annotations

import math
import random

PERIODS_PER_YEAR = 252


def unit_student_t(rng: random.Random, degrees: int) -> float:
    """A Student-t draw scaled to unit variance (fat tails like daily returns)."""
    chi_square = math.fsum(rng.gauss(0.0, 1.0) ** 2 for _ in range(degrees))
    return rng.gauss(0.0, 1.0) / math.sqrt(chi_square / degrees) * math.sqrt((degrees - 2) / degrees)


def no_skill_matrix(periods: int, strategies: int, volatility: float = 0.01, degrees: int = 5,
                    seed: int = 0) -> list[list[float]]:
    """A periods x strategies matrix of independent zero-mean Student-t returns."""
    if degrees <= 2:
        raise ValueError("degrees must exceed 2 for a finite variance")
    rng = random.Random(seed)
    return [[volatility * unit_student_t(rng, degrees) for _ in range(strategies)]
            for _ in range(periods)]


def add_skill(matrix: list[list[float]], strategy: int, annual_sharpe: float,
              volatility: float = 0.01) -> list[list[float]]:
    """A copy of ``matrix`` where one strategy earns a constant edge with the given annual Sharpe ratio."""
    edge = annual_sharpe / math.sqrt(PERIODS_PER_YEAR) * volatility
    return [[value + edge if column == strategy else value for column, value in enumerate(row)]
            for row in matrix]


def columns(matrix: list[list[float]]) -> list[list[float]]:
    """The strategies of a periods x strategies matrix as separate return series."""
    return [list(column) for column in zip(*matrix)]
