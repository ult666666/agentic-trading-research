# Pre-Decision Event Ledger

Status: version 1.0 diagnostic shadow foundation implemented 2026-07-10. It is not a production
decision, price, scoring, risk, freeze, metric, strategy, broker, or execution authority.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## Purpose

The catalyst graph is currently built only after a successful governance-v2 freeze. That is safe
for production, but it conditions the research sample on workflow success and omits rejected,
conflicted, No-Trade, incomplete, and timeout runs. The pre-decision ledger creates an auditable
denominator without rewriting a report or changing a past decision.

Version 1.0 is intentionally isolated at:

```text
data/runtime/predecision_ledger_shadow/<target>/events.jsonl
```

The production research ledger, v1 shadow ledger, reports, candidates, risk results, and freeze
receipts are never read or changed by this writer.

## Contract

[`schemas/predecision_event.schema.json`](../schemas/predecision_event.schema.json) and
[`scripts/event_ledger.py`](../scripts/event_ledger.py) define three row types:

- `source_receipt`: proves when a source artifact was observed, even if its content repeats;
- `event_observation`: records one event state at a specific pre-decision stage;
- `run_terminal`: preserves the denominator when there are zero valid event rows or the workflow
  is incomplete, timed out, failed, or plan-only.

Every line has a contiguous sequence, previous-record hash, record hash, exact field set, and the
non-authority safety block. A torn tail, modified line, missing parent, mixed trading-date
partition, duplicate observation ID, or branching correction invalidates the ledger.

The record contains no event prose, webpage body, raw response, current price, quote, account,
position, cash, credential, or order field. Economic facts are represented by normalized hashes
and source-artifact lineage. The current evidence bridge hashes the validated `evidence.json`
artifact bytes; it does not mislabel that hash as the original webpage/filing response.

## Three-Layer Identity

Repeated retrieval and economic revisions are separate:

```text
raw_payload_id
  = SHA-256(exact persisted source artifact bytes)

semantic_batch_id
  = SHA-256(normalized semantic payload hash
           + source schema version
           + normalizer version)

observation_receipt_id
  = SHA-256(source ID
           + observed time
           + cutoff
           + retrieval metadata hash
           + raw payload ID)
```

The same raw/semantic content observed twice therefore remains one semantic vintage with two
receipts. A changed source payload becomes a new raw payload. A normalizer version change creates a
new semantic representation while retaining the raw lineage.

`event_id` depends on stable entity/symbol-as-of identity, event type, effective time, and claim
fingerprint, not retrieval time or source. `observation_id` identifies the complete immutable
observation. Macro events may have no security identity.

## Corrections And As-Of Replay

Corrections append a new `event_observation` with `correction_of_observation_id`; an existing line
is never edited. A parent may have one direct correction. A later correction must point to the
current child, producing a linear history instead of ambiguous branches.

An as-of view includes a row only when its observed time, data cutoff, and control-plane append
time are all at or before the requested cutoff. A correction supersedes its parent only after the
correction itself becomes visible. `outcome_eligible_at` cannot precede the original cutoff, and
version 1.0 writes no outcome values.

## Morning DAG Bridge

Every new Morning DAG shadow terminal attempts a deterministic capture:

1. If the adjudicator artifact passes the existing evidence schema and every completed agent hash
   matches the corresponding shadow worker artifact, append one evidence source receipt and the
   linked event observations.
2. If evidence is absent or invalid, append no event row and preserve the validation errors only in
   shadow state.
3. Always append the validated DAG terminal row so a zero-event or failed run remains observable.

This bridge is fail-soft for the shadow experiment. It cannot block or permit the legacy Morning
path. A future production proposal must separately prove and obtain approval for a fail-closed
pre-freeze binding.

## Commands

```bash
python3 scripts/event_ledger.py validate \
  --ledger data/runtime/predecision_ledger_shadow/YYYY-MM-DD/events.jsonl

python3 scripts/event_ledger.py as-of \
  --ledger data/runtime/predecision_ledger_shadow/YYYY-MM-DD/events.jsonl \
  --cutoff YYYY-MM-DDTHH:MM:SSZ

python3 scripts/test_event_ledger.py
```

## Promotion Gate

The ledger must remain diagnostic until all of the following are separately reviewed:

1. at least five valid-window Morning shadow runs preserve every discovered/rejected/conflicted
   event and every zero-event terminal;
2. stable issuer/security/listing identities replace symbol-only identity where available;
3. raw provider receipts distinguish original source bytes from normalized evidence artifacts;
4. correction chains and as-of views pass replay against real source revisions;
5. catalyst graph input is migrated to a ledger cutoff in an isolated diagnostic comparison;
6. an exact protected production patch receives explicit approval.

No ledger result can establish an edge, profitability, Buy authority, or permission to place an
order.
