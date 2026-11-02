"""PSR, DSR and MinTRL against closed forms, a published example and simulations."""
import math
import random
from statistics import NormalDist, fmean
import unittest

from backtest_diagnostics.sharpe import (expected_maximum_sharpe, deflated_sharpe_ratio, minimum_track_record_length,
                                         moments, probabilistic_sharpe_ratio, select_best)
from backtest_diagnostics.synthetic import columns, no_skill_matrix


class SharpeTests(unittest.TestCase):
    def test_moments_by_hand(self):
        summary = moments([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(summary.mean, 3.0)
        self.assertAlmostEqual(summary.stdev, math.sqrt(2.5))
        self.assertAlmostEqual(summary.skewness, 0.0)
        self.assertAlmostEqual(summary.kurtosis, 1.7)  # m4 / m2^2 = 6.8 / 4
        self.assertAlmostEqual(moments([0.0, 0.0, 3.0]).skewness, 1 / math.sqrt(2))
        with self.assertRaises(ValueError):
            moments([1.0, 1.0, 1.0])

    def test_psr_matches_its_closed_form_for_normal_returns(self):
        for sharpe, benchmark, periods in [(0.1, 0.0, 250), (0.05, 0.02, 1000), (-0.02, 0.0, 60)]:
            z = (sharpe - benchmark) * math.sqrt(periods - 1) / math.sqrt(1 + sharpe ** 2 / 2)
            self.assertAlmostEqual(probabilistic_sharpe_ratio(sharpe, benchmark, periods), NormalDist().cdf(z))
        self.assertAlmostEqual(probabilistic_sharpe_ratio(0.1, 0.1, 500, -1.0, 6.0), 0.5)
        # Negative skew and fat tails widen the estimator's spread, which lowers PSR for SR > SR*.
        self.assertLess(probabilistic_sharpe_ratio(0.1, 0.0, 500, -2.0, 9.0), probabilistic_sharpe_ratio(0.1, 0.0, 500))

    def test_deflated_sharpe_ratio_reproduces_the_paper_example(self):
        # Inputs of the numerical example in Bailey and Lopez de Prado (2014): 100 trials, an
        # annualized SR of 2.5 over 1,250 daily returns, skewness -3, kurtosis 10, and an
        # annualized variance of the trials' Sharpe ratios of 0.5 (250 days a year).
        sharpe, trial_variance = 2.5 / math.sqrt(250), 0.5 / 250
        self.assertAlmostEqual(expected_maximum_sharpe(100, trial_variance), 0.1132, places=4)
        self.assertAlmostEqual(deflated_sharpe_ratio(sharpe, 1250, 100, trial_variance, -3.0, 10.0), 0.9004, places=4)

    def test_minimum_track_record_length_inverts_psr(self):
        for sharpe, skew, kurt in [(0.1, 0.0, 3.0), (0.08, -1.0, 7.0), (0.2, 0.5, 4.0)]:
            length = minimum_track_record_length(sharpe, 0.0, skew, kurt, 0.95)
            self.assertAlmostEqual(probabilistic_sharpe_ratio(sharpe, 0.0, length, skew, kurt), 0.95, places=12)
        self.assertEqual(minimum_track_record_length(0.0, 0.0), math.inf)

    def test_expected_maximum_matches_simulated_maxima(self):
        rng = random.Random(3)
        for trials, runs in [(10, 4000), (100, 1500), (1000, 300)]:
            simulated = fmean(max(rng.gauss(0.0, 1.0) for _ in range(trials)) for _ in range(runs))
            # The formula is an approximation; it stays within 3% of the simulated mean maximum.
            self.assertLess(abs(expected_maximum_sharpe(trials, 1.0) / simulated - 1), 0.03, trials)

    def test_psr_is_calibrated_for_a_strategy_chosen_in_advance(self):
        # No skill, no selection: PSR >= 0.95 should happen in about 5% of runs, also with fat tails.
        matrix = no_skill_matrix(250, 1600, seed=11)
        values = [moments(column) for column in columns(matrix)]
        rate = sum(probabilistic_sharpe_ratio(v.sharpe, 0.0, v.periods, v.skewness, v.kurtosis) >= 0.95
                   for v in values) / len(values)
        self.assertLess(abs(rate - 0.05), 3 * math.sqrt(0.05 * 0.95 / len(values)), rate)

    def test_selection_inflates_psr_and_dsr_corrects_it(self):
        report = select_best(columns(no_skill_matrix(500, 50, seed=5)))
        self.assertGreater(report.psr, 0.95)
        self.assertLess(report.dsr, 0.95)
        self.assertGreater(report.expected_maximum_sharpe, 0)
        with self.assertRaises(ValueError):
            select_best([[0.1, 0.2, 0.3]])


if __name__ == "__main__":
    unittest.main()
