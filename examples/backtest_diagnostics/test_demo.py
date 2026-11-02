"""The synthetic demo reaches its stated verdicts, and selection bias shows up in the pass rates."""
import contextlib
import io
import unittest

from backtest_diagnostics.calibration import pass_rates
import overfitting_demo


class DemoTests(unittest.TestCase):
    def test_demo_flags_the_no_skill_winner_and_passes_the_skilled_strategy(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = overfitting_demo.main([])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("Every diagnostic flags the no-skill winner and passes the injected skill.", output.getvalue())

    def test_selection_bias_appears_in_psr_and_not_in_dsr(self):
        rates = pass_rates(30, seed=2000)
        self.assertGreater(rates["PSR, best of N"], 0.3)
        self.assertLess(rates["DSR, best of N"], 0.15)
        self.assertLess(abs(rates["mean PBO"] - 0.5), 0.15)
        skilled = pass_rates(10, annual_sharpe=4.0, seed=3000)
        self.assertGreater(skilled["DSR, best of N"], 0.7)
        self.assertGreater(skilled["Reality Check p < 0.05"], 0.7)


if __name__ == "__main__":
    unittest.main()
