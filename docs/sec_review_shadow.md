# SEC Document-Review Shadow Coordinator

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

`scripts/sec_review_shadow.py` connects the immutable SEC data-lake intake to the existing
metadata parser and filing-document review sealer. It is deliberately a **shadow research lane**:
it measures whether deterministic SEC review can run reliably before any result is allowed near
production evidence adjudication.

The coordinator does not fetch SEC documents and does not invoke a model. It performs no broker,
portfolio, order, scoring, classification, freeze, ledger, or metrics operation.

## Pipeline

1. Read `data/runtime/data_lake/readiness/<target>.json`.
2. Validate the readiness receipt and select its exact `sec_edgar` batch.
3. Validate the immutable lake manifest and reconstruct the normalized provider batch.
4. Publish `source_batch.json` once.
5. Build and publish the cutoff-aware `events.json` once with
   `scripts/sec_event_parser.py`.
6. Build and publish the complete-denominator `queue.json` once with
   `scripts/sec_document_review.py`.
7. Optionally accept the SEC agent's untrusted `result_draft.json`.
8. Revalidate the readiness receipt, lake manifest, reconstructed source batch, event bundle,
   queue, and draft; then deterministically publish `result.json` once.
9. Atomically replace `status.json` with the current waiting, ready, or at-issue receipt.

Canonical target paths:

```text
data/runtime/research/<target>/sec_review_shadow/source_batch.json
data/runtime/research/<target>/sec_review_shadow/events.json
data/runtime/research/<target>/sec_review_shadow/queue.json
data/runtime/research/<target>/sec_review_shadow/result_draft.json
data/runtime/research/<target>/sec_review_shadow/result.json
data/runtime/research/<target>/sec_review_shadow/status.json
```

`source_batch.json`, `events.json`, `queue.json`, and `result.json` are immutable. Re-running the
same input is idempotent; different content at an existing path is an `at_issue` conflict.
`status.json` is replaceable because it is a health receipt, not source evidence.
Readiness or manifest drift after queue preparation also blocks sealing; a queue cannot outlive
the provenance chain recorded by its preparation receipt without an explicit new target/replay.

## Commands

Validate all bound contracts:

```bash
python3 scripts/sec_review_shadow.py check
```

Prepare the target queue from the canonical readiness receipt:

```bash
python3 scripts/sec_review_shadow.py prepare --target YYYY-MM-DD
```

For a controlled replay, an explicit cutoff may select the latest valid SEC batch at or before
that timestamp. This bypasses readiness selection only for replay/testing; future cutoffs fail.

```bash
python3 scripts/sec_review_shadow.py prepare \
  --target YYYY-MM-DD \
  --cutoff 2026-07-10T19:00:00Z
```

Seal an existing agent draft:

```bash
python3 scripts/sec_review_shadow.py seal --target YYYY-MM-DD
```

Inspect without publishing a core artifact:

```bash
python3 scripts/sec_review_shadow.py health --target YYYY-MM-DD
```

Validate a status receipt and every referenced immutable artifact:

```bash
python3 scripts/sec_review_shadow.py validate \
  --input data/runtime/research/YYYY-MM-DD/sec_review_shadow/status.json
```

## States

| State | Meaning |
|---|---|
| `waiting_for_sec_batch` | No validated SEC batch is available at the cutoff. No queue is fabricated. |
| `queue_ready` | Source, event bundle, and complete review queue are valid and immutable. |
| `waiting_for_review_draft` | Queue exists, but the SEC agent has not written a draft. |
| `review_draft_ready` | A complete draft passes deterministic validation but is not yet sealed. |
| `result_sealed` | The write-once result was deterministically rebuilt from queue + draft. |
| `at_issue` | A receipt, manifest, timestamp, denominator, draft, hash, or immutable artifact failed validation. |

Waiting is not evidence of absence and is not a successful review. `at_issue` fails closed within
the shadow lane. Morning-run integration is fail-soft so this experimental lane cannot suppress,
upgrade, or otherwise alter the established research workflow.

## Agent Draft Contract

The `sec-ir-verification-agent` must persist its normal agent artifact first. If queue work fits
inside the existing time budget, it may additionally write only
`sec_document_review_result_draft/1.0` at `$TRADING_SEC_REVIEW_RESULT_DRAFT`.

Requirements:

- Exactly one review per queue task, including tasks that could not be reached.
- Unreached or inaccessible tasks use `unavailable` plus an allowlisted reason.
- Non-unavailable reviews require SEC Archive receipts, retrieval timestamps, content SHA-256,
  complete mandatory checks, and the full required-fact denominator.
- Missing, unavailable, conflicted, and present values remain distinct.
- Raw source bodies are not persisted.
- Instructions embedded in filings are ignored as untrusted data.
- The draft cannot add authority, Buy, score, freeze, strategy, metric, portfolio, order, or
  execution fields.

The sealer validates the draft structurally and semantically. It does not trust the model's
declared status; status and summaries are deterministically re-derived.

## Authority Boundary

Every coordinator artifact states:

```text
research_only=true
diagnostic_only=true
shadow_only=true
execution_enabled=false
feeds_production=false
feeds_buy_gate=false
feeds_production_scoring=false
feeds_freeze_gate=false
grants_catalyst_confirmation=false
grants_source_authority=false
counts_in_metrics=false
broker_access=none
```

A sealed result is still untrusted for production. It is not read by `evidence-adjudicator`, does
not count toward quorum, and cannot affect a report or candidate. Enabling consumption requires a
separate, reviewed deterministic adjudication bridge and an explicit governance change.

## Operations

SEC intake requires a policy-valid descriptive `SEC_USER_AGENT` plus the explicit local
`SEC_USER_AGENT_APPROVED=1` transmission-consent marker. Configure both with
`scripts/setup_sec_user_agent.sh`; see [`docs/sec_access.md`](sec_access.md). Missing identity yields
`sec_collection_missing_required_env`; malformed identity, absent consent, or unsafe local storage
yields `sec_collection_invalid_required_env`. Both map to the canonical `waiting_for_sec_batch`
state. The system does not perform an ad hoc network fetch or backfill.

Run the focused regressions with:

```bash
python3 scripts/test_sec_event_parser.py
python3 scripts/test_sec_document_review.py
python3 scripts/test_sec_review_shadow.py
python3 scripts/test_prompt_discipline.py
```

## Adoption and Quality Rollup

`scripts/sec_review_shadow_rollup.py` validates up to 20 status/result chains and writes the
replaceable post-close artifact `data/runtime/health/sec_review_shadow_rollup.json`:

```bash
python3 scripts/sec_review_shadow_rollup.py run \
  --limit 20 --to YYYY-MM-DD --expected-target YYYY-MM-DD \
  --out data/runtime/health/sec_review_shadow_rollup.json
```

It reports queue/draft/seal adoption, complete task/review volume, result/disposition counts,
receipt-backed and unavailable-review rates, prompt-injection counts, and invalid/at-issue
sessions. It never reads broker data or mutates an input. Missing expected-target status is an
explicit invalid observation; missing SEC intake capability remains an honest waiting state.

The versioned checkpoint requires at least 20 queue sessions and 100 sealed reviews. Reaching it
only opens a human source/excerpt semantic-quality review. Counts, hashes, and receipt presence do
not establish extraction accuracy, profitability, or authorization to build/activate an evidence
bridge. `bridge_activation_allowed` remains false in policy and every rollup artifact.

Rollback is mechanical: remove the fail-soft prepare block from `scripts/run_morning_scan.sh` and
the shadow instructions from the morning prompt/SEC agent. Existing immutable artifacts may remain
as diagnostics because no production consumer reads them.
