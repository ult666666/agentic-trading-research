"""CSCV PBO against a brute-force reference, a hand example and simulated behaviour."""
from itertools import combinations
import math
from statistics import fmean, stdev
import unittest

from backtest_diagnostics.cscv import probability_of_backtest_overfitting
from backtest_diagnostics.synthetic import add_skill, no_skill_matrix


def reference_pbo(matrix, blocks):
    """Direct CSCV: rebuild every in-sample and out-of-sample matrix and rank with sorting."""
    size = len(matrix) // blocks
    parts = [matrix[b * size:(b + 1) * size] for b in range(blocks)]
    strategies = len(matrix[0])
    logits = []
    for chosen in combinations(range(blocks), blocks // 2):
        inside = [row for b in chosen for row in parts[b]]
        outside = [row for b in range(blocks) if b not in chosen for row in parts[b]]
        in_sharpe = [fmean(r[s] for r in inside) / stdev([r[s] for r in inside]) for s in range(strategies)]
        out_sharpe = [fmean(r[s] for r in outside) / stdev([r[s] for r in outside]) for s in range(strategies)]
        winner = in_sharpe.index(max(in_sharpe))
        order = sorted(range(strategies), key=out_sharpe.__getitem__)
        rank = order.index(winner) + 1
        relative = rank / (strategies + 1)
        logits.append(math.log(relative / (1 - relative)))
    return sum(value <= 0 for value in logits) / len(logits), logits


class OverfittingTests(unittest.TestCase):
    def test_matches_a_brute_force_reference(self):
        for seed in range(6):
            matrix = no_skill_matrix(240, 7, seed=seed)
            if seed % 2:
                matrix = add_skill(matrix, 2, 2.0)
            for blocks in (4, 6, 8):
                with self.subTest(seed=seed, blocks=blocks):
                    expected, logits = reference_pbo(matrix, blocks)
                    report = probability_of_backtest_overfitting(matrix, blocks=blocks)
                    self.assertAlmostEqual(report.pbo, expected)
                    self.assertEqual(report.splits, math.comb(blocks, blocks // 2))
                    for mine, theirs in zip(report.logits, logits):
                        self.assertAlmostEqual(mine, theirs)

    def test_hand_example(self):
        # Two blocks of two rows. Strategy 0 wins block 0 and loses block 1; strategy 1 the reverse,
        # so the in-sample winner is always last out of sample: rank 1 of 2, logit ln(1/2) < 0.
        matrix = [[0.03, 0.00], [0.01, 0.01], [-0.02, 0.02], [0.00, 0.04]]
        report = probability_of_backtest_overfitting(matrix, blocks=2)
        self.assertEqual(report.splits, 2)
        self.assertEqual(report.pbo, 1.0)
        for value in report.logits:
            self.assertAlmostEqual(value, math.log(1 / 2))

    def test_no_skill_is_a_coin_flip_and_persistent_skill_is_not(self):
        rates = [probability_of_backtest_overfitting(no_skill_matrix(400, 20, seed=s), blocks=10).pbo
                 for s in range(30)]
        self.assertLess(abs(fmean(rates) - 0.5), 0.12)
        skilled = add_skill(no_skill_matrix(400, 20, seed=99), 5, 6.0)
        report = probability_of_backtest_overfitting(skilled, blocks=10)
        self.assertEqual(report.pbo, 0.0)
        self.assertEqual(report.probability_of_loss, 0.0)

    def test_strategy_order_does_not_matter(self):
        matrix = no_skill_matrix(300, 9, seed=4)
        reversed_columns = [list(reversed(row)) for row in matrix]
        self.assertAlmostEqual(probability_of_backtest_overfitting(matrix, blocks=6).pbo,
                               probability_of_backtest_overfitting(reversed_columns, blocks=6).pbo)

    def test_rejects_unusable_shapes(self):
        matrix = no_skill_matrix(60, 3, seed=1)
        for blocks in (3, 0, 7, 40):
            with self.assertRaises(ValueError):
                probability_of_backtest_overfitting(matrix, blocks=blocks)
        with self.assertRaises(ValueError):
            probability_of_backtest_overfitting([[0.1], [0.2], [0.3], [0.4]], blocks=2)


if __name__ == "__main__":
    unittest.main()
