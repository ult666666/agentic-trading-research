# Market Regime

## Evidence

Use a combination of:

- SPY and QQQ trend over intraday, 5-day, 20-day, and 50-day windows.
- Price relative to important moving averages when data supports them.
- Advance/decline or breadth proxies.
- Sector participation.
- Volatility proxies.
- Credit, rates, and macro-event context when material.
- Distribution or accumulation behavior.
- Whether current movement is caused by a scheduled event.

## Classification

### RISK_ON_TREND
Broad indexes and participation are constructive; leadership is reasonably broad; pullbacks are controlled.

### RISK_OFF_TREND
Indexes, breadth, or leadership deteriorate persistently; defensive assets or sectors dominate.

### RANGE_BOUND
Indexes oscillate without sustained follow-through; breakouts fail frequently.

### HIGH_VOLATILITY
Large ranges, gaps, unstable correlations, wide spreads, or rapidly changing news dominate.

### EVENT_DRIVEN
A scheduled or breaking event dominates price formation, such as central-bank decisions or major macro releases.

### INSUFFICIENT_DATA
Evidence is stale, incomplete, or contradictory.

## Exposure response

- RISK_ON_TREND: normal configured limits.
- RANGE_BOUND: smaller size, higher entry selectivity, faster reassessment.
- RISK_OFF_TREND: favor cash or only exceptional relative-strength setups.
- HIGH_VOLATILITY: reduce size substantially or remain in cash.
- EVENT_DRIVEN: wait for evidence after the event unless the trade explicitly models event risk.
- INSUFFICIENT_DATA: no trade.

Do not force a classification merely to produce activity.
