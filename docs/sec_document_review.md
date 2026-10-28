# SEC Document Review Queue and Result Seal

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

`scripts/sec_document_review.py` is the deterministic boundary between SEC filing metadata
discovery and document-level verification. It consumes a validated
`sec_filing_event_bundle/1.0`, creates one review task for every eligible filing, and seals an
untrusted agent draft only after strict structural, provenance, and safety checks.

This layer is diagnostic. It is not wired into production scoring, the Buy gate, the freeze
gate, research metrics, or execution.

## Why this layer exists

The SEC metadata parser can establish that an accepted filing and item code existed by the
knowledge cutoff. It cannot establish what the document actually says. The review queue keeps
that distinction explicit:

1. `sec_event_parser.py` discovers metadata and applies the point-in-time cutoff.
2. `sec_document_review.py build-queue` creates a complete filing-review denominator.
3. `sec-ir-verification-agent` retrieves SEC documents and writes an untrusted result draft.
4. `sec_document_review.py seal-result` validates and hash-binds the draft.
5. A later evidence-adjudication step may normalize sealed findings into auditable, untrusted
   evidence claims. They remain non-authoritative without a separate trusted production receipt
   validator registered in the control plane.

Neither step 3 nor step 4 grants catalyst confirmation. The current sealed review and
`source_content_receipt/1.0` are both shadow-only. The evidence adjudicator and source matrix may
organize the records, but only `scripts/evidence_schema.py` plus a registered production receipt
validator can grant authority; the registry is currently empty.

## Official SEC paths

The queue constructs URLs only from a validated CIK, accession number, and safe primary
document filename. SEC documents for an accession are located under:

```text
https://www.sec.gov/Archives/edgar/data/<CIK-without-leading-zeroes>/<accession-without-dashes>/
```

The queue includes the filing detail page, directory `index.json`, filing directory, and the
primary document URL when the metadata supplied one. Result receipts must use exactly those
URLs or a safe, flat exhibit filename in the same accession directory. Other domains, query
strings, fragments, credentials, encoded paths, path traversal, and mismatched document kinds
are rejected.

SEC documents the archive layout in its
[Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
guide. SEC's [Developer Resources](https://www.sec.gov/about/developer-resources) currently
state a maximum of 10 requests per second and require identifiable automated clients. This
project's policy is more conservative: 5 requests per second and a descriptive user agent.
The queue builder and result sealer themselves make no network request.

## Queue contract

`build-queue` validates the complete upstream event bundle, including its original provider
batch, policy, taxonomy, and hashes. It then creates exactly one task per eligible filing.

Each task contains:

- accession, CIK, symbol, form, acceptance time, and upstream filing hash;
- deterministic SEC Archive URLs;
- critical, high, or normal priority with machine-readable reasons;
- standalone, supplemental, and unknown item codes;
- a complete list of required fact fields;
- mandatory checks and prohibited inferences;
- a self-integrity hash.

Critical examples include bankruptcy, material cybersecurity incidents, listing risk,
unregistered equity sales, and financial-statement non-reliance. Registration statements,
prospectuses, effectiveness notices, post-effective amendments, and other material financing
forms are high priority. Priority affects review ordering only; it does not establish market
direction or actionability.

## Draft and seal contract

The agent writes `sec_document_review_result_draft/1.0`. The draft must contain one review for
every queue task — including capped tasks. The daily shadow cap (`shadow_daily_task_cap` in
`config/sec_document_review_policy.json`, default 10; token-optimization 2026-07-23) bounds
ACTIVE document retrieval only: every task beyond the cap is still reported, as `unavailable`
with could-not-verify code `deferred_by_cap`, and the deterministic coordinator flags cap
engagement in `status.json` reason codes. The task denominator never shrinks and deferred
tasks are never silently omitted. A non-`unavailable` review must include:

- every mandatory check;
- at least one SEC source receipt with retrieval time and content SHA-256;
- exactly one explicit fact state for every required fact field;
- `present`, `missing`, `unavailable`, or `conflicted` value semantics;
- exact excerpts and receipt-backed citations for present/conflicted values;
- an explicit dilution/financing-risk state;
- a prompt-injection detection flag.

Missing and unavailable values must be `null`; they cannot be silently converted to zero or
false. Numeric zero and boolean false remain valid present values. An unavailable review has
no receipts or facts and must provide a controlled reason code.

The sealer derives receipt, fact, review, and artifact hashes; binds the queue and original
draft files; derives the overall `completed`, `partial`, or `failed` status; and publishes
write-once. Validation rebuilds the sealed artifact from the queue and draft, so changing and
re-hashing only the result cannot pass.

The sealer verifies that a content hash is present and correctly formatted. It does not
re-download content or prove that an extracted fact semantically matches the source. That is
why `model_output_is_untrusted=true`, `adjudication_required=true`, and
`grants_catalyst_confirmation=false` remain fixed.

For Form 4/Form 4-A Ownership XML, `scripts/sec_form4_parser.py` can create a separate offline
`sec_form4_transaction_bundle/1.0` before model review. It removes owner PII and footnote text,
checks transaction-code/direction consistency, and preserves filed amounts and ownership state.
It does not replace this review contract: code `P` still means open-market or private purchase,
footnotes remain unresolved, and the normalized bundle grants no insider-purchase or catalyst
confirmation. Production integration requires a separate reviewed change.

## Commands

```bash
python3 scripts/sec_document_review.py check

python3 scripts/sec_document_review.py build-queue \
  --events /path/to/sec_filing_events.json \
  --target 2026-07-10 \
  --out /path/to/sec_document_review_queue.json

python3 scripts/sec_document_review.py validate-queue \
  --input /path/to/sec_document_review_queue.json

python3 scripts/sec_document_review.py seal-result \
  --queue /path/to/sec_document_review_queue.json \
  --draft /path/to/sec_document_review_result_draft.json \
  --out /path/to/sec_document_review_result.json

python3 scripts/sec_document_review.py validate-result \
  --input /path/to/sec_document_review_result.json

python3 scripts/test_sec_document_review.py
```

## Integration boundary

Future integration should provide the queue path to `sec-ir-verification-agent`, require a
sealed result before the agent may claim completion, and give only the sealed result to the
evidence adjudicator. That integration must be a separate reviewed change. It must not let a
queue, draft, or sealed review bypass claim normalization, source authority, independent
corroboration, conflict preservation, risk veto, workflow-quality, freeze, or Buy gates.
