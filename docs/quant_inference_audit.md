# Dependence-Aware Quant Inference Audit

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: version 1.0 is implemented as an offline diagnostic. It audits normalized portfolio daily
returns and train/test label intervals. It does not change the research factory, promotion policy,
strategy, risk limits, reports, Buy authority, broker permissions, or execution state.

Authority: `AGENTS.md` overrides this document. Machine semantics are fixed by
`config/quant_inference_policy.json` and implemented by
[`scripts/quant/inference_audit.py`](../scripts/quant/inference_audit.py).

## Why This Exists

Per-trade observations are not independent when positions overlap, signals cluster on the same
event date, or returns persist across sessions. An IID standard error can therefore overstate the
effective sample and make a weak result look precise. Random cross-validation can also leak an
outcome interval or issuer group into both training and test data.

This layer adds four separate diagnostics:

1. Bartlett-kernel Newey-West HAC standard error for the mean daily portfolio return;
2. deterministic moving overlapping-block bootstrap confidence interval;
3. positive-autocorrelation effective sample size;
4. interval overlap, shared-group, and post-test embargo purging.

## HAC Contract

For daily returns `x_t`, mean `m`, sample size `n`, lag `L`, and biased autocovariance
`gamma_l = sum((x_t-m)(x_(t-l)-m))/n`:

```text
long_run_variance = gamma_0 + 2 * sum((1 - l/(L+1)) * gamma_l, l=1..L)
standard_error_mean = sqrt(long_run_variance / n)
```

The versioned lag rule is:

```text
L = floor(4 * (n / 100)^(2 / 9)), capped at n - 1
```

At least 30 daily observations are required. Zero or negative estimated long-run variance is
reported as unavailable/degenerate, never converted into a zero standard error.

Newey and West introduced a positive semi-definite HAC covariance estimator under general
heteroskedasticity and autocorrelation conditions:
https://www.nber.org/papers/t0055

## Moving-Block Bootstrap

The audit forms all overlapping consecutive blocks of length:

```text
B = ceil(cube_root(n)), capped at n
```

It draws blocks with replacement until each bootstrap sample contains `n` observations, trims the
final block, and records 2,000 sample means. The 95% interval uses type-7 linear-interpolated
percentiles. Seed `1729` is fixed in policy, making identical inputs replay byte-for-byte.

At least 60 daily observations are required. The block bootstrap preserves short-range sequence
within sampled blocks; it does not prove stationarity or choose an optimal block length. Kunsch's
general stationary-observation bootstrap work is the methodological basis:
https://doi.org/10.1214/aos/1176347265

## Effective Sample Size

The diagnostic computes autocorrelations through the HAC lag and uses only positive values in a
conservative denominator:

```text
effective_n = n / (1 + 2 * sum(max(0, rho_l)))
```

The result is capped to `[1, n]`. It is a warning-scale diagnostic, not a universally consistent
estimator and not a factory gate.

## Purge and Embargo

`quant_interval_assignments/1.0` requires every observation's event date, label start/end dates,
group ID, and fixed train/test assignment. The audit removes a training observation when any of
these independently applies:

- its label interval overlaps any test label interval;
- its group appears in the test set;
- its event date occurs after a test label end and within the configured 10-calendar-day embargo.

The output retains the complete source denominator and a separate reason list for every purged
row. An independent second pass verifies that every retained training row is overlap-, group-, and
embargo-free.

## Outputs and Limits

`quant_inference_audit/1.0` reports the raw and effective sample, arithmetic/compound return,
autocorrelations, naive IID standard error, HAC result, moving-block result, and hashes. Status is:

- `reviewable`: both HAC and block bootstrap are available;
- `insufficient_sample`: one or both sample minima are unmet;
- `degenerate`: the sample is large enough but variance-based inference is unavailable.

Within `quant.inference_audit`, Hansen's Superior Predictive Ability test remains explicitly:

```text
spa_available = false
spa_result = null
reason = not_implemented_requires_separate_reference_validation
```

SPA is a distinct multiple-model test, not another name for a block-bootstrap mean interval. The
separate [`scripts/quant/spa_audit.py`](../scripts/quant/spa_audit.py) now implements a
reference-validated, complete-family SPA boundary over aligned portfolio paths; it does not alter
this artifact, the factory, or any promotion gate. See
[`docs/quant_spa_audit.md`](quant_spa_audit.md).

No positive bound from this module establishes live profitability. The source portfolio remains
hypothetical, microstructure inputs remain incomplete, and every output is excluded from official
performance metrics.

## Commands

Validate policy:

```bash
python3 scripts/quant/inference_audit.py check
```

Audit a portfolio result:

```bash
python3 scripts/quant/inference_audit.py audit-portfolio \
  --portfolio <portfolio-result.json> \
  --out <new-inference-audit.json>
python3 scripts/quant/inference_audit.py validate-inference \
  --artifact <new-inference-audit.json>
```

Audit fixed train/test intervals:

```bash
python3 scripts/quant/inference_audit.py audit-intervals \
  --assignments <interval-assignments.json> \
  --out <new-purge-audit.json>
python3 scripts/quant/inference_audit.py validate-purge \
  --artifact <new-purge-audit.json>
```

Outputs are atomic and never overwrite an existing artifact.

## Current Honest State

Reference-direction, serial-dependence, deterministic bootstrap, purge, tamper, and CLI fixtures
are implemented. The live research lake still lacks enough approved point-in-time observations to
produce a reviewable portfolio inference audit. Scheduled factory gates are unchanged; wiring this
diagnostic into governance requires real data, independent review, and a separate approved change.
