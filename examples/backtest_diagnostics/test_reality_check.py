"""Stationary bootstrap, Reality Check and SPA: reference computations and null behaviour."""
import math
import random
from statistics import NormalDist, fmean
import unittest

from backtest_diagnostics.reality_check import (bootstrap_means, data_snooping_tests, resample_rows,
                                                stationary_blocks)
from backtest_diagnostics.synthetic import add_skill, no_skill_matrix


class BootstrapTests(unittest.TestCase):
    def test_blocks_cover_every_period_with_the_requested_mean_length(self):
        rng = random.Random(1)
        lengths = []
        for _ in range(300):
            blocks = stationary_blocks(500, 8.0, rng)
            self.assertEqual(sum(length for _, length in blocks), 500)
            self.assertTrue(all(0 <= start < 500 and length >= 1 for start, length in blocks))
            lengths.extend(length for _, length in blocks[:-1])  # the last block is cut to fit
        self.assertLess(abs(fmean(lengths) / 8.0 - 1), 0.05)
        self.assertEqual({length for _, length in stationary_blocks(50, 1.0, rng)}, {1})

    def test_prefix_sums_equal_means_over_the_resampled_rows(self):
        matrix = no_skill_matrix(97, 4, seed=2)
        means = bootstrap_means(matrix, 25, 6.0, seed=9)
        rng = random.Random(9)
        for sample in means:
            rows = resample_rows(97, stationary_blocks(97, 6.0, rng))
            self.assertEqual(len(rows), 97)
            for strategy, value in enumerate(sample):
                self.assertAlmostEqual(value, fmean(matrix[row][strategy] for row in rows), places=12)


class SnoopingTests(unittest.TestCase):
    def test_one_strategy_with_iid_resampling_matches_a_one_sided_mean_test(self):
        rng = random.Random(4)
        data = [[rng.gauss(0.12, 1.0)] for _ in range(400)]
        reality, _ = data_snooping_tests(data, resamples=4000, mean_block=1.0, seed=3)
        values = [row[0] for row in data]
        mean, sd = fmean(values), math.sqrt(fmean((v - fmean(values)) ** 2 for v in values))
        expected = 1 - NormalDist().cdf(mean * math.sqrt(len(values)) / sd)
        self.assertLess(abs(reality.p_value - expected), 0.01, (reality.p_value, expected))

    def test_skill_is_found_and_hopeless_strategies_do_not_hide_it(self):
        matrix = add_skill(no_skill_matrix(500, 30, seed=8), 7, 4.0)
        # Ten clearly losing strategies: the Reality Check stays valid, SPA ignores them.
        matrix = [[value - 0.003 if 20 <= k else value for k, value in enumerate(row)] for row in matrix]
        reality, spa = data_snooping_tests(matrix, resamples=499, mean_block=10.0, seed=1)
        self.assertEqual((reality.best, spa.best), (7, 7))
        self.assertLess(reality.p_value, 0.01)
        self.assertLessEqual(spa.p_value, reality.p_value)

    def test_no_strategy_beating_the_benchmark_gives_large_p_values(self):
        matrix = [[value - 0.002 for value in row] for row in no_skill_matrix(300, 10, seed=6)]
        reality, spa = data_snooping_tests(matrix, resamples=199, seed=2)
        self.assertGreater(reality.p_value, 0.5)
        self.assertEqual(spa.statistic, 0.0)
        self.assertEqual(spa.p_value, 1.0)

    def test_rejection_rates_under_the_null(self):
        # 60 no-skill universes: rejection at 5% must stay near 5% (3 standard errors).
        runs, rejected = 60, {"Reality Check": 0, "SPA": 0}
        for run in range(runs):
            reality, spa = data_snooping_tests(no_skill_matrix(250, 10, seed=500 + run), resamples=199, seed=run)
            rejected["Reality Check"] += reality.p_value < 0.05
            rejected["SPA"] += spa.p_value < 0.05
        bound = 0.05 + 3 * math.sqrt(0.05 * 0.95 / runs)
        for name, count in rejected.items():
            self.assertLessEqual(count / runs, bound, name)

    def test_rejects_unusable_inputs(self):
        with self.assertRaises(ValueError):
            bootstrap_means([[0.1], [0.2]], 10, 2.0)
        with self.assertRaises(ValueError):
            stationary_blocks(10, 0.5, random.Random(0))


if __name__ == "__main__":
    unittest.main()
