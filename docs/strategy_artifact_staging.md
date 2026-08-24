# Current-Run Strategy Artifact Staging

Status: implemented in the unattended post-close chain. The normal current terminal is
`not_candidate` because the point-in-time data lake and forward sample are not ready.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. Staging is offline research
preservation only. It cannot register, promote, activate, score, recommend, or execute a strategy;
it imports no broker, account, order, model, network, or registry mutation surface.

## Why a separate controller exists

A bridge-ready grid bundle is not enough to deserve durable comparison staging. The independent
research factory may still reject it for weak walk-forward evidence, holdout performance after
modeled costs, drawdown, Deflated Sharpe, or probability of backtest overfitting. Conversely, the
factory scans immutable historical bundles, so an older candidate must not make today's rejected
bundle appear approved.

[`scripts/quant/strategy_artifact_stager.py`](../scripts/quant/strategy_artifact_stager.py)
therefore runs after both systems and requires all of the following:

1. the grid-worker and factory health documents validate and carry the exact target and run ID;
2. the grid health names a `bundle_created` or `bundle_existing` current-run bundle;
3. that exact relative bundle path appears exactly once in the factory experiment list;
4. that exact experiment, not merely an older one, is
   `candidate_for_category_D_governance_review` with every gate passed;
5. the grid, factory metrics, and bundle document carry the same bundle SHA-256; and
6. the WP-22 bridge independently reports the bundle `ready`.

Any stale, malformed, duplicated, path-escaping, hash-drifting, or authority-conflicting input
fails closed. A valid noncandidate is not an incident and writes no package.

## State machine

- `not_candidate`: current inputs are valid, but the exact current bundle did not receive the
  factory review-candidate terminal. No staging was attempted.
- `staged`: the candidate, rederived SPY total-return reference, and export receipt were atomically
  written to a new package and then independently reverified.
- `verified_existing`: the exact package already existed and every file was reverified; no file
  was overwritten.
- `blocked`: structural, provenance, bundle-identity, bridge, or package verification failed.

One replaceable current-run health artifact is written at
`data/runtime/health/<target>_strategy_artifact_stager.json`. Its Draft 2020-12 contract is
[`schemas/quant_strategy_artifact_stager.schema.json`](../schemas/quant_strategy_artifact_stager.schema.json).
Success requires the source-health hashes, bundle path/hash, package path, export receipt hash,
candidate result hash, benchmark result hash, and zero blockers. Every terminal permanently says
`registry_mutated=false`, `performance_evaluated=false`, and
`profitability_status=not_established`.

## Idempotent package verification

`strategy_artifacts.verify_export_package` does not trust the presence of a directory. It requires
exactly three regular, nonsymlink files; compares the staged candidate byte-for-byte with the
manifest-bound factory source; validates the candidate; validates and fully rederives the SPY
benchmark from the original adjusted view and policies; validates the receipt; and reconstructs
the expected exported receipt exactly. Extra files, source drift, a resealed benchmark change, or
receipt drift blocks the rerun without deleting or overwriting evidence.

Packages remain under:

```text
data/runtime/quant/strategy_artifacts/<bundle-id>/
  candidate_portfolio.json
  spy_benchmark_reference.json
  export_receipt.json
```

The append-only [`data/quant_strategy_registry.jsonl`](../data/quant_strategy_registry.jsonl) is
outside this controller. It remains genesis-only and requires separate governed registration.

## Operations

The scheduled post-close wrapper supplies one shared current-run ID and invokes:

```bash
python3 scripts/quant/strategy_artifact_stager.py run \
  --target YYYY-MM-DD --run-id postclose-... \
  --grid-health data/runtime/health/YYYY-MM-DD_quant_grid_worker.json \
  --factory-health data/runtime/health/YYYY-MM-DD_research_factory.json \
  --experiment-root data/runtime/experiments \
  --output-root data/runtime/quant/strategy_artifacts \
  --out data/runtime/health/YYYY-MM-DD_strategy_artifact_stager.json --replace
```

Validate a health artifact without writing:

```bash
python3 scripts/quant/strategy_artifact_stager.py validate \
  --path data/runtime/health/YYYY-MM-DD_strategy_artifact_stager.json
```

The next genuine boundary is data/provider authority and later explicit strategy registration.
Once a real cohort exists, the offline
[`strategy evidence gate`](strategy_evidence_gate.md) can join this exact package to uniqueness,
dependence-aware inference, and SPA. It still cannot register or start a shadow strategy. No
boundary should be bypassed by reconstructing data, relaxing evidence gates, or inferring approval
from a staged package.
