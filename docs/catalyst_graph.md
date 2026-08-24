# Catalyst Event Graph

Status: deterministic Layer-2 foundation implemented 2026-07-09. The graph is diagnostic-only and
does not feed the Buy gate or production scorer. Structurally valid evidence that fails solely
because generic source claims lack a trusted production receipt may still produce a
`diagnostic_only_unverified` graph; the production report remains blocked.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. A graph verdict describes evidence
lineage, not trade direction, expected return, suitability, or permission to act.

## Why It Exists

Narrative catalyst labels are easy to misjoin. Before this layer, a catalyst did not have to name
its symbol or exact supporting claims; two events for one symbol, or a symbol-less event, could
therefore borrow unrelated evidence. The canonical evidence contract now requires:

- an uppercase `symbol` for every single-name event;
- `symbol=null` only for macro/sector events;
- a non-empty `claim_ids` list naming the exact evidence claims supporting that event;
- every linked claim to match the event symbol and expected claim category.

Missing or mismatched joins invalidate the evidence artifact and fail closed. Historical frozen
reports are not rewritten; older evidence without these fields will fail a new validation or graph
build explicitly.

## Artifacts

- [`config/catalyst_taxonomy.json`](../config/catalyst_taxonomy.json): versioned exact aliases,
  event categories, and binary/dilution flags.
- [`scripts/catalyst_graph.py`](../scripts/catalyst_graph.py): pure deterministic builder,
  validator, immutable writer, and summary command.
- [`scripts/evidence_schema.py`](../scripts/evidence_schema.py): authority for exact catalyst-to-
  claim joins and catalyst quorum.
- `data/runtime/research/<target>/catalyst_graph.json`: untracked `catalyst_graph/1.0` artifact.

The graph binds itself to SHA-256 hashes of both `evidence.json` and the taxonomy. A byte change to
either input invalidates validation. Rebuilding from identical inputs is idempotent; publishing
different bytes to an existing graph path is refused.

## Graph Model

Node types:

- `issuer`: target-session symbol proxy for the issuer;
- `security`: target-session symbol identity;
- `event`: normalized catalyst event with source label explicitly marked untrusted data;
- `source_document`: one exact evidence claim, represented by domain/type/tier/timestamps and a
  reference back to `evidence.json`, not copied webpage text. Each node records
  `catalyst_authority_status` and the deterministic denial reason.

Edge types:

- `affects`, `issued_by`, `issues` for event/entity structure;
- `confirms`, `supports`, `contradicts` for evidence relations;
- `syndicated_copy` for claims sharing one `corroboration_group`.

Common-origin copies never become independent corroboration. Explicit claim conflicts are retained
as `contradicts` edges when both endpoints are present.

## Verdicts

Each event receives exactly one evidence verdict:

- `confirmed`: an explicitly linked primary claim has been revalidated by a registered trusted
  production receipt validator and no linked unresolved conflict remains;
- `partial`: linked qualifying secondary evidence exists but no effective primary exists;
- `conflicted`: the event or any linked claim has unresolved material conflict;
- `unverified`: no qualifying linked evidence exists or the event remains rumored.

A model-authored `validation_status=validated`, plausible SEC accession, official-looking domain,
or shadow `source_content_receipt/1.0` cannot produce `confirmed` or `partial`. Until a trusted
production collector is registered, those source nodes remain `lead_only`, their relations never
use `confirms`, and the event verdict is `unverified` unless a conflict lowers it further.

Separate flags preserve `issuer_only`, `stale_or_expired`, `rumored`, `binary_event`,
`dilution_financing`, and `unresolved_conflict`. `already_priced` remains `null` because the current
structured evidence cannot establish it deterministically. No prose inference fills that gap.

A `confirmed` event may still be bearish, stale, dilutive, binary, illiquid, fully priced, or
otherwise unsuitable. Every verdict carries `grants_actionability=false`.

## Freeze Integration

[`scripts/research_freeze.py`](../scripts/research_freeze.py) builds the graph only after the report,
freeze receipt, research-ledger result, and structured-candidate artifact are complete. Graph
generation is best-effort and cannot reclassify the report or alter any authority. A graph failure
is returned as a diagnostic error; it never weakens a gate.

This ordering is deliberate for the first rollout. Making graph health a production gate would be
a separate change-controlled proposal requiring replay evidence and explicit approval.

An isolated pre-freeze denominator now exists in the
[`pre-decision event ledger`](predecision_event_ledger.md). The current graph does not consume it:
moving graph input from frozen evidence to an as-of ledger view remains a diagnostic comparison and
separate governance step. This preserves production behavior while rejected, conflicted,
No-Trade, incomplete, and timeout observations begin accumulating outside production metrics.

## Commands

```bash
python3 scripts/catalyst_graph.py build --target YYYY-MM-DD
python3 scripts/catalyst_graph.py validate \
  --graph data/runtime/research/YYYY-MM-DD/catalyst_graph.json
python3 scripts/catalyst_graph.py summary \
  --graph data/runtime/research/YYYY-MM-DD/catalyst_graph.json
python3 scripts/test_catalyst_graph.py
```

The builder makes no HTTP, model, broker, or ledger call. It reads normalized evidence and writes
one immutable derived artifact only.
