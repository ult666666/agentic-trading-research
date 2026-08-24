# Risk Policy

## Default limits

Apply these defaults unless the user explicitly chooses stricter limits:

- Maximum simultaneous positions: 2
- Maximum allocation to one position: 50% of current account value
- Maximum total invested capital: 80%
- Minimum cash reserve: 20%
- Maximum modeled loss per trade: the lesser of 2.5% of account value or $1.00
- Maximum session loss: 5% of session-starting account value
- Maximum drawdown from session high-water mark: 6%
- Maximum new entries per session: 3
- Minimum expected reward-to-risk ratio: 2.0
- Minimum stock price: $3.00
- New entries: regular US market hours only
- Do not initiate a new position in the first five minutes after the regular open
- Do not initiate a new position after 3:30 p.m. Eastern Time

## Position sizing

For a long position:

- `stop_distance = entry_price - invalidation_price`
- `stop_distance_pct = stop_distance / entry_price`
- `risk_budget = min(account_value * max_trade_risk_pct, max_trade_risk_dollars)`
- `position_by_risk = risk_budget / stop_distance_pct`
- Final position is the minimum of:
  - position_by_risk
  - account_value × maximum position allocation
  - verified buying power
  - any user-imposed cap

Reject zero, negative, nonfinite, contradictory, or implausibly precise inputs.

## Hard rejects

Reject when any condition is true:

- Account state, quote time, security identity, buying power, or positions are not freshly verified.
- Security is outside the permitted universe.
- Proposed order requires margin or leverage.
- Price is below the configured minimum.
- Fractional eligibility is required but unverified.
- Position, portfolio exposure, cash reserve, or loss budget would be exceeded.
- Session loss or high-water-mark drawdown limit has been reached.
- Reward-to-risk is below minimum.
- More than two positions would result.
- Entry count is exhausted.
- Proposal averages down.
- Quote is stale.
- Catalyst is unverified.
- Security is halted, materially illiquid, or spread is excessive.
- Intended order conflicts with an existing pending order.
- Any numeric field is missing, malformed, negative where prohibited, or nonfinite.

## Important limitation

A modeled invalidation loss is not guaranteed. Gaps, halts, slippage, partial fills, and order-type limitations can create larger losses. State this whenever relevant.
