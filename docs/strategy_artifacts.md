# Strategy Artifact Bridge

## Purpose

`scripts/quant/strategy_artifacts.py` closes two research-governance gaps between the research
factory and the strategy comparison registry:

1. it derives a deterministic SPY total-return reference with the same cost-policy,
   portfolio-policy, stress-multiplier, and date denominator as a candidate portfolio; and
2. it exports a factory candidate only when the portfolio inputs and export intent were locked
   before holdout evaluation.

This is offline research infrastructure. It does not establish profitability, alter the research
factory, register or promote a strategy, read account data, or create an actual order simulation.

## SPY benchmark contract

The builder accepts a hash-valid `quant_portfolio_backtest/1.0` candidate and a hash-valid
`adjusted_bar_view/1.0` whose view is exactly `total_return`. It refuses raw/split-only views,
missing SPY bars, missing candidate dates, malformed sources, or a candidate whose cost and
portfolio policy hashes/versions do not match the supplied policies.

The comparison path uses normalized initial NAV 1.0:

- allocate `maximum_gross_exposure` to SPY at the first candidate date's adjusted open;
- subtract `minimum_cost_bps_per_side * candidate_cost_stress_multiplier` on entry;
- mark the position at each matching adjusted close without filling missing dates;
- exit at the final candidate date's adjusted close and subtract the same per-side cost; and
- retain the candidate's exact ordered daily-date denominator.

The SPY reference intentionally exempts the single passive reference from candidate position,
sector, catalyst, and stop-risk caps. This does not make SPY a candidate portfolio. The initial
allocation still uses the candidate portfolio's maximum gross exposure so market exposure is not
silently levered.

Every output embeds the aligned SPY bars, policy snapshots, candidate hashes, cost calculation,
date list, source signal-plan hash, portfolio-plan hash, and lineage. The benchmark inherits both
plan hashes from the candidate so a same-named candidate produced from a changed replay recipe is
not treated as the same comparison. `validate-benchmark` independently rederives decisions, NAV,
returns, exposure, costs, turnover, drawdown, and summary. `verify-benchmark` rebuilds the complete
artifact from external sources and requires byte-equivalent canonical JSON.

## Factory preregistration contract

A factory bundle is exportable only if it contains a `portfolio_export` section with exactly:

- `contract`: a `quant_factory_portfolio_export_contract/1.0` input artifact;
- `candidate_portfolio`: the resulting portfolio artifact; and
- `source_backtest`: the exact holdout or declared cost-stress backtest used by that portfolio.

The preregistered contract contains no outcome. Before the holdout lock it fixes:

- source bars and complete source plans;
- the complete portfolio allocation plan;
- cost and portfolio policies;
- the total-return benchmark view; and
- the cost-stress multiplier.

The contract and every input hash must appear in the experiment manifest's `data_snapshots`.
Because the source backtest and candidate do not exist until evaluation, the bundle's export
section binds them afterward. The bridge then verifies all file/object lineage, exact stress-source
selection, candidate result hashes, policy contracts, manifest binding, and benchmark source.

Legacy bundles do not contain this contract. They deterministically produce
`blocked_missing_preregistered_portfolio_export_contract`; the bridge never invents a portfolio
plan after seeing holdout outcomes. Invalid timestamps, omitted manifest hashes, path traversal,
symlinks, drift, malformed artifacts, or lineage mismatches produce
`blocked_invalid_preregistered_portfolio_export_contract` before export.

## Export package

`export-bundle` creates one write-once package atomically:

```text
data/runtime/quant/strategy_artifacts/<bundle-id>/
  candidate_portfolio.json
  spy_benchmark_reference.json
  export_receipt.json
```

The receipt records the preregistration lock, source refs, exported file/result hashes, and these
permanent facts:

- `registry_mutated=false`
- `performance_evaluated=false`
- `profitability_status=not_established`
- `execution_enabled=false`

The receipt schema is closed at every nested level. A ready/exported receipt must contain the
complete preregistration binding flags, exact source refs, exact exported refs, lock timestamps,
and a sorted unique blocker list. Unknown nested keys, placeholders, false binding flags,
candidate/source hash disagreement, or a receipt that was modified and re-hashed are rejected by
both runtime semantic validation and the strict JSON Schema.

The package is only staging material. Registering a benchmark/candidate remains a separate,
explicit append to `data/quant_strategy_registry.jsonl`; this tool never opens that file.

## Commands

```bash
python3 scripts/quant/strategy_artifacts.py check

python3 scripts/quant/strategy_artifacts.py build-benchmark \
  --candidate /path/to/candidate_portfolio.json \
  --adjusted-view /path/to/total_return_view.json \
  --out /path/to/spy_benchmark_reference.json

python3 scripts/quant/strategy_artifacts.py verify-benchmark \
  --artifact /path/to/spy_benchmark_reference.json \
  --candidate /path/to/candidate_portfolio.json \
  --adjusted-view /path/to/total_return_view.json

python3 scripts/quant/strategy_artifacts.py inspect-bundle \
  --bundle /path/to/experiment/bundle.json \
  --experiment-root /path/to/experiments \
  --out /path/to/export_preflight_receipt.json

python3 scripts/quant/strategy_artifacts.py export-bundle \
  --bundle /path/to/experiment/bundle.json \
  --experiment-root /path/to/experiments
```

All artifact writes are no-overwrite. No command invokes a model, network, broker, account, order,
production score, risk gate, Buy gate, freeze gate, or performance ledger.

## Current state and next integration

The grid worker's `0.2.0` program path now performs the factory-side integration. Before any
backtest, it materializes every variant's full and holdout plans, cost-stress plans, complete
portfolio plan, total-return benchmark view, and export contract; all are fingerprinted in one
manifest. After deterministic variant selection it runs a fresh manifest-bound holdout backtest,
builds the selected candidate portfolio, and refuses to publish unless this bridge reports
`ready`. Canonical JSON serialization makes each file SHA exactly equal to its contract/object SHA.

Legacy bundles remain blocked and are never retrofitted. The repository strategy registry is still
genesis-only, the scheduled worker has not yet produced a real bundle because the point-in-time
lake and SPY corporate-action coverage are incomplete, and no real package has been exported. A
current-run post-close controller now stages a package idempotently only after the independent
factory gives that exact bundle its strongest category-D review-candidate terminal. It fully
revalidates an existing package instead of overwriting it. Registration, uniqueness review,
promotion, strategy activation, and execution remain separate governed actions. See
[`docs/strategy_artifact_staging.md`](strategy_artifact_staging.md).
