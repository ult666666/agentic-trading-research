# Zero-Dollar Source Health

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

scripts/free_source_health.py turns immutable 05:00 intake receipts into one cross-session,
hash-validated collection-health artifact:

    data/runtime/free_source_health/current.json

It answers whether each deterministic free source has actually been collected reliably. It does
not inspect raw provider payloads, contact the network, invoke a model or broker, or infer missing
records. It is observability only and has no evidence, source, live-price, scoring, risk, Buy,
freeze, metrics, strategy, profitability, account, broker, or execution authority.

## Monitored Sources

The source list is derived from config/free_source_ensemble.json and config/data_lake.json; it is
not duplicated in the health config. The current contract requires nine exact automatic data-lake
sources:

- **public zero-key:** BLS observations, BLS release calendar, CFTC COT, Federal Reserve H.15,
  Nasdaq current directory, Nasdaq listing events, and Nasdaq ex-date notices;
- **conditional identity:** SEC EDGAR, which is reported separately as
  manual_identity_required when the approved User-Agent identity is absent;
- **local archive:** local point-in-time bars, reported separately as local_history_pending while
  history is still accumulating.

Optional free-key providers, web research, and the read-only broker lane are excluded because this
receipt measures deterministic data-lake collection, not every research input.

## Integrity Chain

For each selected session, the rollup:

1. loads data/runtime/data_lake/readiness/<target>.json;
2. recomputes its receipt_sha256 and validates its safety flags and collection counts;
3. loads data/runtime/free_source_ensemble/<target>/plan.json;
4. recomputes plan_sha256, validates the redacted plan, and confirms the target;
5. maps each source to its exact data-lake provider and preserves failures, partial errors,
   missing identity, and local-history waiting states;
6. writes one atomic, self-hashed free_source_health/1.0 receipt.

A malformed or hash-mismatched session is counted as invalid and blocks the rollup. It is never
silently dropped. Credential values, raw responses, account data, prices, positions, balances,
model prose, and order instructions are not accepted.

## Status Semantics

- collecting: fewer than five valid sessions exist; rates are not mature.
- healthy: at least five valid sessions exist and no monitored public/identity lane is at issue.
- degraded: the receipt is valid, but a public provider is degraded/partial or SEC identity is
  missing.
- blocked: the health config or any selected receipt/plan integrity chain is invalid.

Provider labels are independent:

- unobserved, collecting, healthy, degraded, or partial;
- manual_identity_required for SEC identity absence;
- local_history_pending for an archive that has not accumulated usable bars.

ok=true means the artifact and at least one selected session validated. It does not mean every
provider is healthy and never means research-ready or Buy-authorized.

## Production Wiring

- scripts/run_data_intake.sh validates the health contract during dry-run and refreshes the receipt
  after successful live intake.
- scripts/run_post_close_review.sh refreshes the same aggregate before narrative review.
- prompts/morning_scan.md, prompts/post_close_review.md, and prompts/run_post_close.md allow only
  aggregate health fields to be rendered.
- scripts/autonomy_service_health.py embeds a reduced, validated projection under
  foundation_safety.free_source_health. This projection does not affect foundation_safety.status
  and keeps production_gate_enforced=false.
- scripts/free_source_health_alert.py converts validated conditions into state-deduplicated local
  alert/recovery decisions. Shell wrappers own fixed local notification text; see
  docs/free_source_health_alerts.md.

Both wrapper calls are fail-soft. A nonzero health result is logged and shown honestly, but it
cannot change data-lake readiness or any research/trading authority.

## Commands

    # Cross-check config and source mappings
    python3 scripts/free_source_health.py --check

    # Build or refresh the production aggregate
    python3 scripts/free_source_health.py \
      --out data/runtime/free_source_health/current.json

    # Build a bounded diagnostic window
    python3 scripts/free_source_health.py --limit 5 --out /tmp/free-source-health.json

    # Adversarial contract tests
    python3 scripts/test_free_source_health.py

On a new installation with no intake sessions, the command intentionally exits nonzero with
status=collecting. After the first valid intake it may exit zero while remaining collecting; five
valid sessions are required before a healthy rate label is possible.

## Operator Response

- manual_actions identifies only truly manual prerequisites, currently SEC identity setup.
- recommendations identifies collection repairs or additional-session needs.
- Public-provider consecutive failures should be investigated from automation/error logs.
- A new/changed incident and its recovery notify once locally. Unchanged incidents, collecting,
  and local-history accumulation are intentionally silent.
- Invalid sessions require receipt/plan integrity reconciliation; do not delete or rewrite
  immutable lake batches to make the health label green.

This layer improves failure visibility and source diversity management. It does not demonstrate an
edge or profitability.
