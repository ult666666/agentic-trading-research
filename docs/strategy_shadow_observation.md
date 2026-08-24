# Prospective Strategy Shadow Observation

## Purpose

`scripts/quant/strategy_shadow_monitor.py` collects future-only, hypothetical strategy
observations after a candidate has passed `quant_strategy_evidence_gate/1.0` and a human has
explicitly approved a shadow plan. It measures whether the candidate continues to add
net-of-modeled-cost information beyond SPY and the complete frozen incumbent cohort.

This is research infrastructure. It cannot register, promote, activate, trade, access a broker,
change risk, feed the Buy/freeze gates, or establish profitability.

## Trust Boundary

The plan freezes:

- the exact evidence-gate file and semantic hash;
- cohort, registry-head, uniqueness-result, and source-set hashes;
- candidate, benchmark, and every incumbent source file;
- variant, plan version, source signal-plan hash, portfolio-plan hash, cost stress,
  cost/portfolio policy, and conservative-rule contracts;
- inference, uniqueness, artifact, and market-calendar policy hashes;
- future start date, fixed checkpoints, alpha schedule, maximum horizon, and missing-session rule.

Production CLI commands use the local UTC wall clock. They expose no `created-at`, `recorded-at`,
`occurred-at`, or `as-of` override. Deterministic time injection is accepted only by the Python
test seam in a non-production repository root.

## Return Provenance

A complete draft cannot accept a return scalar. The caller supplies one standard
`quant_portfolio_backtest/1.0` result per frozen strategy. The return bundle:

1. validates each standard result and its hashes;
2. requires the final two rows to be the previous and target US market sessions;
3. re-derives terminal net return from normalized NAV;
4. checks the result's terminal `daily_return` recurrence;
5. revalidates the frozen strategy/cost contract, including the `1.1` replay-spec hash over the
   exact source signal plan, portfolio plan, policy hashes, stress, and conservative rules;
6. semantically rebuilds the SPY benchmark from its embedded adjusted bars and policies;
7. binds every source file, daily path, terminal row, and result hash.

The SPY contract inherits the candidate's source signal-plan and portfolio-plan hashes. A future
portfolio result with a changed plan cannot enter the ledger merely by retaining the old strategy
name or by recomputing its outer result hash. Re-sealing a modified plan is also rejected when the
plan is rederived from the frozen evidence dossier.

Unavailable data becomes an explicit `unavailable` draft with reason codes. It never becomes a
zero return and cannot be silently omitted.

## Forward-engine status

[`scripts/quant/strategy_shadow_forward.py`](../scripts/quant/strategy_shadow_forward.py) now
implements the missing stateful, session-by-session generator contract: frozen replay specs,
continuous candidate/incumbent/benchmark state, one-time modeled costs, conservative daily-bar
exit semantics, complete versus unavailable denominators, full prior-state replay, and a
caller-frozen roster run receipt. Its timestamps and roster are local self-commitments, not
external proof of prospective creation, exact plan approval, or protection from rebuilding the
entire spec set. See
[`docs/strategy_shadow_forward.md`](strategy_shadow_forward.md).

That engine is intentionally standalone in the current release. The monitor commands below still
accept the legacy hash-bound `quant_portfolio_backtest/1.0` source bundle. A later, separately
tested schema migration must freeze the new replay-spec hashes inside the human-approved plan and
make the monitor accept only the corresponding forward-run/result contracts. Until that migration,
the existence of the engine does not make a real shadow plan operational or scheduled.

## Append And Audit

The ledger is write-once JSONL with an exclusive append lock, monotonic trusted timestamps,
sequential market sessions, previous-event hashes, `fsync`, and a maximum size/horizon. A complete
event must be recorded within the configured post-close deadline; late complete backfill is
rejected, while a late explicit unavailable marker is permitted.

Append performs full semantic source validation. Routine summaries re-check the complete hash
chain plus draft, bundle, and source-file bindings without repeating the expensive benchmark
rebuild. `validate-ledger` performs the full semantic audit when required.

## Optional-Stopping Control

Every summary enumerates all market sessions whose close is at or before its trusted `as_of` time.
If the ledger lacks either a complete or unavailable event for any such session,
`session_denominator_complete` fails and the summary is ineligible for supportive review. A
favorable 60-session checkpoint therefore cannot remain supportive merely by stopping collection
before session 61 is recorded.

Descriptive checkpoints are 20 and 40 sessions. Decision looks are fixed at 60, 120, 180, and 252
sessions with a pre-registered two-sided alpha schedule totaling at most 0.05. Collection is capped
at 504 sessions. No rolling best-window selection is allowed.

## Workflow

All output paths must be under `data/runtime/quant/strategy_shadow/` and all input artifacts under
the configured runtime allowlist.

```bash
python3 scripts/quant/strategy_shadow_monitor.py plan \
  --evidence-gate data/runtime/quant/strategy_reviews/<id>/evidence_gate.json \
  --start-date YYYY-MM-DD \
  --approval-reference approval:<human-reference> \
  --out data/runtime/quant/strategy_shadow/<plan-id>/plan.json

python3 scripts/quant/strategy_shadow_monitor.py init \
  --plan data/runtime/quant/strategy_shadow/<plan-id>/plan.json \
  --ledger data/runtime/quant/strategy_shadow/<plan-id>/events.jsonl

python3 scripts/quant/strategy_shadow_monitor.py bundle \
  --plan data/runtime/quant/strategy_shadow/<plan-id>/plan.json \
  --session YYYY-MM-DD \
  --candidate-result data/runtime/quant/<candidate-result>.json \
  --benchmark-result data/runtime/quant/<benchmark-result>.json \
  --incumbent-result '<strategy-id>=data/runtime/quant/<incumbent-result>.json' \
  --out data/runtime/quant/strategy_shadow/<plan-id>/bundles/YYYY-MM-DD.json

python3 scripts/quant/strategy_shadow_monitor.py draft \
  --plan data/runtime/quant/strategy_shadow/<plan-id>/plan.json \
  --session YYYY-MM-DD --state complete \
  --bundle data/runtime/quant/strategy_shadow/<plan-id>/bundles/YYYY-MM-DD.json \
  --out data/runtime/quant/strategy_shadow/<plan-id>/drafts/YYYY-MM-DD.json

python3 scripts/quant/strategy_shadow_monitor.py append \
  --plan data/runtime/quant/strategy_shadow/<plan-id>/plan.json \
  --ledger data/runtime/quant/strategy_shadow/<plan-id>/events.jsonl \
  --draft data/runtime/quant/strategy_shadow/<plan-id>/drafts/YYYY-MM-DD.json

python3 scripts/quant/strategy_shadow_monitor.py summarize \
  --plan data/runtime/quant/strategy_shadow/<plan-id>/plan.json \
  --ledger data/runtime/quant/strategy_shadow/<plan-id>/events.jsonl \
  --out data/runtime/quant/strategy_shadow/<plan-id>/summary.json
```

For an unavailable session, omit `--bundle`, use `--state unavailable`, and provide at least one
`--reason <reason-code>`.

## Interpretation

`shadow_evidence_supportive_for_human_review` means only that all pre-registered diagnostic gates
passed at the latest fixed checkpoint with a complete session denominator. It does not prove future
profit, live fill quality, capacity, causality, or regime persistence. Any production change would
require a separate human decision, system-change record, rollback plan, and the project's existing
governance gates.
