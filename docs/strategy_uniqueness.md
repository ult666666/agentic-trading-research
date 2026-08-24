# Strategy Uniqueness and Incremental Alpha Audit

Status: implemented offline diagnostic; not scheduled; not a research-factory or production gate.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. The audit never establishes
future profitability, activates a strategy, changes a score or risk rule, reads account data, or
creates an order. Its strongest label is `incremental_alpha_review_candidate`, which means only
that a human may consider the evidence in a separate category-D governance review.

## Purpose

The research factory already asks whether a pre-registered strategy survives chronological
out-of-sample testing, a locked holdout, modeled-cost stress, Deflated Sharpe Ratio, and CSCV/PBO.
Those checks do not answer a different question: does a candidate add anything beyond market beta
and strategies already in the library?

[`scripts/quant/strategy_uniqueness.py`](../scripts/quant/strategy_uniqueness.py) answers that
narrow question using only hash-valid `quant_portfolio_backtest/1.0` artifacts. It compares one
candidate with:

1. one explicit benchmark path, normally a point-in-time SPY reference portfolio over the same
   untouched evaluation dates; and
2. one to twenty incumbent strategy portfolio paths produced under compatible cost and holdout
   assumptions.

All source daily returns are already net of each portfolio artifact's modeled costs. Cost-stress
multiplier, cost-policy version/hash, and portfolio-policy version/hash must match across every
source. The audit intersects dates rather than filling missing values, requires that intersection
to cover at least 95% of every path, discloses every dropped observation, embeds the aligned return
panel, binds every source and policy by SHA-256, and re-derives every statistic during validation.

## What Is Measured

### Dependence and duplicated timing

The audit reports Pearson and Spearman return correlation against the benchmark and each
incumbent. It also computes candidate/incumbent active-day Jaccard overlap from end-of-day gross
exposure. Under the v1 policy, any incumbent return correlation above `0.80` in absolute value or
active-day overlap above `0.75` classifies a sufficiently observed candidate as `redundant`.

Active-day overlap uses end-of-day gross exposure from daily portfolio paths. It cannot detect an
intraday-only or same-session round trip, so it remains a coarse duplication diagnostic.

These are clone filters, not alpha estimators. Low correlation can arise from noise, sparse risk,
or bad data and never proves an economic edge.

### Benchmark and incumbent spanning

For the common-date sample, the audit estimates:

```text
candidate_t = alpha + beta_benchmark * benchmark_t
              + beta_incumbent * equal_weight_incumbent_composite_t + residual_t
```

It computes a Bartlett-kernel Newey-West HAC standard error for the per-observation intercept using
the fixed lag rule in the policy. The lower 95% confidence bound must be strictly above zero. A
singular factor matrix, constant residual, short sample, or nonpositive HAC variance remains
unavailable and cannot pass.

This interval is not adjusted for model selection. A positive result still requires the factory's
separate locked holdout, DSR, and PBO evidence. The audit deliberately does not annualize Sharpe
or manufacture a p-value.

### Incremental portfolio utility

The baseline is the equal-weight daily return of all incumbents. The augmented diagnostic is equal
weight across the candidate and every incumbent. The audit compares:

- mean daily return;
- Sharpe per observation, not annualized;
- compounded maximum drawdown; and
- 5% historical expected shortfall.

The candidate must not dilute mean return, must improve Sharpe by at least `0.05`, may not worsen
maximum drawdown by more than `0.02`, and may not worsen daily expected shortfall by more than
`0.001`. These fixed allocation rules are diagnostics, not portfolio recommendations.

## Classifications

| Label | Meaning |
|---|---|
| `insufficient_data` | Common dates, active observations, dependence measures, HAC regression, or incremental Sharpe are not reviewable. |
| `redundant` | The candidate duplicates a source result, exceeds incumbent return correlation, or exceeds active-day overlap. |
| `distinct_but_unproven` | It looks different, but residual alpha or incremental risk-adjusted utility misses at least one hurdle. |
| `incremental_alpha_review_candidate` | All diagnostic hurdles pass; category-D human review is allowed, but activation and profitability claims remain prohibited. |

The shipped policy requires at least 60 aligned observations, 20 candidate-active observations,
and 95% common-date coverage for every source.
Threshold weakening fails policy validation. Missing or malformed portfolio returns, future-dated
observations, duplicate strategy IDs, source hash drift, semantic output tampering, and overwrite
attempts all fail closed.

## Commands

Check the policy without writing:

```bash
python3 scripts/quant/strategy_uniqueness.py check
```

Run one attended offline audit:

```bash
python3 scripts/quant/strategy_uniqueness.py audit \
  --candidate /path/to/candidate_portfolio.json \
  --benchmark /path/to/spy_reference_portfolio.json \
  --incumbent incumbent-v1=/path/to/incumbent_v1_portfolio.json \
  --candidate-id candidate-v2 \
  --benchmark-id SPY \
  --as-of 2026-07-11T23:00:00Z \
  --out /tmp/strategy_uniqueness.json
```

Validate and semantically re-derive a result:

```bash
python3 scripts/quant/strategy_uniqueness.py validate \
  --artifact /tmp/strategy_uniqueness.json
```

## Integration Boundary and Current State

The audit is not imported by `quant.research_factory`, any morning/post-close wrapper, production
scoring, evidence quorum, risk, freeze, or metrics code. It is consumed only by the offline
[`strategy evidence gate`](strategy_evidence_gate.md), which also requires the factory, staged
package, dependence-aware inference, and candidate-specific SPA evidence. A real use still
requires compatible benchmark, candidate, and incumbent portfolio paths over the same untouched
holdout with approved point-in-time data and historical cost inputs. The repository does not yet
contain that mature empirical panel, so no real strategy has received any of these labels.

The offline registry and cohort-locking boundary now exists in
[`docs/strategy_registry_and_cohorts.md`](strategy_registry_and_cohorts.md). It freezes benchmark,
incumbent-library, source, date, and cost-contract selection before invoking this audit. The live
registry remains empty. The evidence gate now provides the hash-bound review dossier that joins
this result to DSR/PBO and the other diagnostics, but it cannot register, shadow, promote, or
activate a strategy.

## Statistical References

- Whitney Newey and Kenneth West, "A Simple, Positive Semi-definite, Heteroskedasticity and
  Autocorrelation Consistent Covariance Matrix," NBER Technical Working Paper 55.
- David H. Bailey and Marcos Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for Selection
  Bias, Backtest Overfitting, and Non-Normality."
- David H. Bailey et al., "The Probability of Backtest Overfitting."

The references motivate the uncertainty and selection-bias posture. They do not validate this
project's strategy, data, thresholds, or future returns.
