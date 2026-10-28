# Security-Master Rollforward

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

The rollforward accumulates official Nasdaq Trader add/delete observations and reconciles them
against the latest current symbol directory. It improves future point-in-time auditability without
claiming that a current directory reconstructs the past.

This component is research-only and shadow-only. It has no broker access and cannot grant complete
history, universe eligibility, production scoring, strategy activation, Buy authority, or
execution.

## Inputs and Output

Inputs are immutable, hash-validated lake batches:

- `listing_events` from `nasdaq_listing_events`;
- the latest cutoff-eligible `security_master` snapshot from `nasdaq_trader_public`.

The derived receipt is written atomically to:

```text
data/runtime/security_master_rollforward/current.json
```

Every run is reproducible from the lake and a UTC knowledge cutoff. Repeated observations of the
same `event_id` are deduplicated; a changed payload under the same ID is a conflict, not an
overwrite.

## Episode Semantics

- `active_from_event`: an observed add with no later delete;
- `closed_from_events`: an observed add and later delete;
- `deleted_start_unknown`: a delete was observed but the listing start predates available events;
- `active_start_unknown`: the current directory contains the symbol but no observed add exists.

Unknown dates remain `null`. Duplicate adds, same-day add/delete pairs, current symbols observed
after a delete, missing current symbols after an effective add, invalid manifests, and conflicting
event payloads remain explicit blockers or conflicts.

## Acceptance Boundary

`config/security_master_rollforward.json` records the target history start, current-universe
breadth, required add/delete observations, conflict tolerance, and seed-attestation state. The
shipped contract pins `historical_seed_attestation_present=false`, and the validator rejects a
local mutation to `true`. The rollforward therefore reports only:

- `accumulating`;
- `seed_attestation_required`; or
- `blocked`.

It can never report a complete-history pass. An external historical seed would require a separate
data-license review, provenance attestation, schema, tests, change-control entry, and explicit human
approval. It must not be smuggled in by editing this config.

## Daily Operation

`scripts/run_data_intake.sh` validates the rollforward contract during dry-run. After a successful
live intake it updates the receipt. A rollforward diagnostic failure is logged fail-soft and cannot
change data-lake authority or turn a degraded intake into ready.

```bash
python3 scripts/security_master_rollforward.py --check
python3 scripts/security_master_rollforward.py \
  --lake-root data/runtime/data_lake/lake \
  --out data/runtime/security_master_rollforward/current.json
python3 scripts/test_security_master_rollforward.py
python3 scripts/test_data_lake_coverage_acceptance.py
```

Nasdaq ex-date exception notices are intentionally excluded. They do not verify action type, cash
amount, adjustment factor, or complete corporate-action coverage.
