# Superior Predictive Ability Audit

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: version 1.1 is an offline, diagnostic-only implementation of Peter Hansen's test for
Superior Predictive Ability (SPA). It compares a complete declared family of modeled strategy
portfolio paths with one explicit benchmark while accounting for model search and serial
dependence. It is not connected to the research factory, production scoring, risk, Buy, freeze,
metrics, brokerage, or execution.

Authority: `AGENTS.md` overrides this document. Machine semantics are fixed by
`config/quant_spa_policy.json` and implemented by
[`scripts/quant/spa_audit.py`](../scripts/quant/spa_audit.py).

## Research basis

The implementation follows Hansen's studentized maximum statistic, stationary-bootstrap
long-run variance estimator, and sample-dependent lower/consistent/upper null distributions:

- Peter R. Hansen, [A Test for Superior Predictive Ability](https://doi.org/10.1198/073500105000000063),
  *Journal of Business & Economic Statistics* 23(4), 365-380.
- Hansen's [working-paper record](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=264569).
- Halbert White, [A Reality Check for Data Snooping](https://doi.org/10.1111/1468-0262.00152),
  *Econometrica* 68(5), 1097-1126.

Hansen's null is that the benchmark is not inferior to any tested alternative. For daily modeled
returns, this module uses `d[k,t] = model[k,t] - benchmark[t]`, so the null is
`max(E[d[k]]) <= 0`. A small p-value rejects that joint null for the supplied family. It does not
identify causality, capacity, persistence, or future profit.

## Input boundary

`build-panel` accepts only independently hash-valid `quant_portfolio_backtest/1.0` artifacts. The
benchmark and every model must have:

- exactly the same ordered daily dates;
- the same cost-stress multiplier plus the same cost and portfolio-policy versions;
- separately disclosed candidate-plan and passive-benchmark-plan versions;
- daily returns consistent with the normalized NAV path;
- long-only normalized capital, zero actual orders, and the source safety contract;
- distinct portfolio IDs and a declared model count.

The output `quant_spa_panel/1.0` records source paths plus file, result, and daily-path hashes. It
does not require a candidate strategy and a passive SPY reference to pretend they use the same
plan: `benchmark_plan` and the per-model `model_plans` are retained separately while cost and
portfolio contracts must match. A
declared count larger than the supplied model count is retained as `family_complete=false`; it is
never repaired or treated as a smaller experiment. Validation rebuilds the entire panel from the
source portfolio artifacts, so rehashing edited returns cannot pass.

The declared count still cannot reveal an experiment the researcher never disclosed. A future
factory integration would also need a preregistered manifest binding the exact model IDs. This
version remains standalone until real compatible daily paths and independent review exist.

## Statistical contract

For model `k`, sample mean `d_bar[k]`, observation count `n`, and stationary-bootstrap long-run
variance `omega2[k]`, the observed statistic is:

```text
T = max(0, max_k(sqrt(n) * d_bar[k] / sqrt(omega2[k])))
```

The stationary bootstrap uses geometrically distributed block lengths. Policy fixes 2,000
replications, seed `260712`, and a primary expected block length of `ceil(sqrt(n))`. The same
uniform draws are reused for sensitivity lengths `0.5x`, `1x`, and `2x` the primary length, so
differences are not caused by unrelated random streams.

The long-run variance uses Hansen's stationary-bootstrap population kernel. A zero or negative
estimate for any supplied model makes that configuration unavailable; a constant positive path
cannot become a zero-variance superiority claim.

Bootstrap samples are recentered three ways:

- `lower`: `max(0, d_bar[k])`;
- `consistent`: retain `d_bar[k]` only when it is above Hansen's
  `-sqrt((omega2[k]/n) * 2*log(log(n)))` relevance threshold, otherwise zero;
- `upper`: retain `d_bar[k]` for every model.

The consistent p-value is primary. The invariant `lower <= consistent <= upper` is enforced.
P-values use Hansen's strict bootstrap-statistic-greater-than-observed rule. A zero value means no
exceedance in 2,000 draws and is explicitly below Monte Carlo resolution, not proof that the true
probability is zero. Monte Carlo standard errors are included.

At least 60 aligned daily observations and two models are required. All three configured block
lengths are always reported. `robust_across_configured_block_lengths=true` means only that the
consistent p-value rejects the supplied joint null at 5% for each configured length.

## Terminals

- `reviewable`: all configured estimates and bootstrap runs are valid;
- `incomplete_family`: supplied model count does not match the declaration;
- `insufficient_sample`: fewer than 60 aligned dates;
- `computational_limit`: the bounded operation budget would be exceeded;
- `degenerate`: at least one configuration has unusable long-run variance.

Every terminal remains a non-actionable diagnostic. Even a reviewable, sensitivity-robust
rejection sets `establishes_profitability=false` and `grants_strategy_promotion=false`.

## Commands

```bash
python3 scripts/quant/spa_audit.py check

python3 scripts/quant/spa_audit.py build-panel \
  --comparison-id research-family-v1 \
  --benchmark /path/to/benchmark-portfolio.json \
  --model /path/to/model-a-portfolio.json \
  --model /path/to/model-b-portfolio.json \
  --declared-models 2 \
  --out /path/to/spa-panel.json

python3 scripts/quant/spa_audit.py audit \
  --panel /path/to/spa-panel.json \
  --out /path/to/spa-audit.json

python3 scripts/quant/spa_audit.py validate-panel --input /path/to/spa-panel.json
python3 scripts/quant/spa_audit.py validate-audit --input /path/to/spa-audit.json
python3 scripts/test_quant_spa_audit.py
```

Outputs are write-once. The module performs no retrieval and imports no model, connector, broker,
dynamic command, or network client.
