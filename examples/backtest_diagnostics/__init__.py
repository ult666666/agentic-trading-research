"""Backtest-overfitting diagnostics for synthetic strategy returns (standard library only).

- ``sharpe``: Probabilistic and Deflated Sharpe Ratios and the minimum track record length.
- ``cscv``: probability of backtest overfitting by combinatorially symmetric cross-validation.
- ``reality_check``: White's Reality Check and Hansen's SPA test with the stationary bootstrap.
- ``synthetic``: no-skill and injected-skill return matrices for the demo and the tests.

Every function takes plain lists of floats and is deterministic for a given seed.
"""
