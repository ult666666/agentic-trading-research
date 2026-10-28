# Strategy Evidence Gate

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: implemented offline and diagnostic-only; not scheduled and not connected to production.
The repository still has no mature real candidate package or locked incumbent cohort, so no real
strategy is currently eligible.

Authority: `AGENTS.md` overrides this document. The strongest possible terminal is
`eligible_for_shadow_observation_review`. That terminal does not start shadow observation, mutate
the strategy registry, change production, establish profitability, create an order, or authorize
execution.

## Purpose

The quant stack previously had strong but separate answers to different questions:

- the research factory checks preregistration, walk-forward behavior, locked holdout results,
  modeled-cost stress, drawdown, Deflated Sharpe Ratio, and CSCV/PBO;
- the artifact stager preserves the exact current-run candidate and SPY reference;
- the cohort audit checks whether the candidate adds information beyond the locked incumbent set;
- the inference audit corrects uncertainty for serial dependence; and
- the SPA audit asks whether the complete declared model family beats its benchmark after model
  search and block-length sensitivity.

[`scripts/quant/strategy_evidence_gate.py`](../scripts/quant/strategy_evidence_gate.py) joins those
contracts without weakening or replacing any of them. Its policy is
`config/quant_strategy_evidence_gate.json`, and its
output contract is
[`schemas/quant_strategy_evidence_gate.schema.json`](../schemas/quant_strategy_evidence_gate.schema.json).

## Required Evidence

All of the following must hold simultaneously:

1. The stager terminal is `staged` or `verified_existing`, and the exact three-file package is
   revalidated against its hashes and semantic contracts. The export receipt must have terminal
   status `exported`, satisfy its closed nested schema, and bind its source candidate hash to the
   exact staged candidate bytes and cost-stress multiplier.
2. The stager's factory-file hash matches the supplied current-run factory artifact, including
   target date, run ID, bundle hash, and the shipped factory-config hash.
3. The exact bundle has the factory's strongest review terminal and all 13 declared evidence gates
   are present and passing; omitting a gate is a failure.
4. At least 2x modeled-cost stress retains positive expectancy.
5. The cohort-bound uniqueness result is `incremental_alpha_review_candidate` under the shipped
   uniqueness policy.
6. Candidate result hashes match across stager, package, uniqueness, inference, and SPA. Benchmark
   hashes match across stager, package, uniqueness, and SPA.
7. The inference audit has at least 60 daily observations, at least 30 effective observations, an
   effective fraction of at least 25%, and strictly positive 95% HAC and moving-block-bootstrap
   lower bounds. The absolute effective-N minimum prevents the fraction rule from admitting a tiny
   dependent sample; the lower fraction accommodates sparse catalyst strategies.
8. SPA uses a complete declared family, is reviewable, rejects robustly across every configured
   block length, and identifies this exact candidate as the best sample model in every
   configuration with a positive mean return differential.

Structural corruption, policy drift, path escape, source-file drift, semantic hash mismatch,
candidate identity drift, or SPA rederivation drift produces `blocked`. Valid evidence that misses
an economic or statistical hurdle produces `not_eligible`. Only zero blockers and zero failed
checks produce `eligible_for_shadow_observation_review`.

## SPY Plan Compatibility

SPA version 1.1 fixes a previously impossible bridge contract. Candidate strategies and a passive
SPY reference must share cost and portfolio-policy versions, but they must not pretend to share a
plan. The panel now records `benchmark_plan` and each entry in `model_plans` separately. Cost,
portfolio, stress, dates, and source hashes remain exact. This allows a real bridge-generated SPY
reference to enter SPA without weakening economic comparability.

## Operations

Inputs are restricted to regular, nonsymlink files under `data/runtime/experiments`,
`data/runtime/health`, and `data/runtime/quant`. Evaluation revalidates every input and writes once:

```bash
python3 scripts/quant/strategy_evidence_gate.py evaluate \
  --stager data/runtime/health/YYYY-MM-DD_strategy_artifact_stager.json \
  --factory data/runtime/health/YYYY-MM-DD_research_factory.json \
  --cohort-audit data/runtime/quant/strategy_reviews/<id>/cohort_audit.json \
  --inference-audit data/runtime/quant/strategy_reviews/<id>/inference_audit.json \
  --spa-panel data/runtime/quant/strategy_reviews/<id>/spa_panel.json \
  --spa-audit data/runtime/quant/strategy_reviews/<id>/spa_audit.json \
  --out data/runtime/quant/strategy_reviews/<id>/evidence_gate.json
```

Check the policy or validate a sealed result without writing:

```bash
python3 scripts/quant/strategy_evidence_gate.py check
python3 scripts/quant/strategy_evidence_gate.py validate \
  --artifact data/runtime/quant/strategy_reviews/<id>/evidence_gate.json
```

The next lifecycle step remains a separate human decision about whether to begin a bounded shadow
observation. Production activation would require new forward evidence, an explicit protected
change, unchanged risk limits, and continued manual order entry.
