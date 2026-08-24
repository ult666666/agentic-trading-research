# Source-content receipt (shadow-only)

`scripts/source_content_receipt.py` closes one narrow but important gap: it can prove that a
bounded byte range came from the exact local representation whose SHA-256 is recorded in the
receipt. It hashes and validates the same in-memory bytes, derives every anchor hash, rejects
duplicate JSON keys and unsafe SEC URLs, and creates fixture receipts with no overwrite. This is
not durable write-once storage: deletion or an external rename remains possible.

It intentionally does **not** solve transport trust yet. The current command is named
`fixture-seal`; it performs no network request and accepts only files below an explicit fixture
root located under the system temporary directory. A same-user process can fabricate such a file.
Consequently every receipt is permanently
marked `shadow_only`, `collector_trust: local_fixture_only`, `feeds_production: false`, and
`grants_source_authority: false`. It cannot validate an evidence claim, confirm a catalyst,
support a Buy, create an Official report, affect metrics, access a broker, or enable execution.

The production bridge stays fail-closed until a control-plane collector can attest the actual
HTTP response bytes and the evidence validator revalidates the receipt, anchor, target, source
role, and content hash. A later signer or append-only external log is also required before the
system may describe receipts as tamper-resistant against another same-user process.

`scripts/source_receipt_manifest.py` adds a separate shadow-only completeness layer for the
current SEC/news collector outputs. It derives the full discovery denominator, records missing and
orphan receipts, reuses each lane validator's receipt-loading contract, and separately checks the
SEC accession/symbol/form or news receipt-ID/URL/symbol/publisher identity. It binds exact local
bytes but does not invoke the claim-level authority grant and deliberately grants no source
authority. Its canonical create-once publication uses a validated same-directory temporary file
and atomic no-replace hard-link commit; an existing final path must be validated separately and is
never treated as an idempotent publish. The sole exception is a fully revalidated interrupted
link-before-unlink state with one final and one controlled temporary sharing the same two-link
inode; ambiguous crash states remain untouched and require quarantine. See
[`docs/source_receipt_manifest.md`](source_receipt_manifest.md). The manifest does not change any
of the transport-trust or semantic-claim limitations above.

## Fixture workflow

```bash
python3 scripts/source_content_receipt.py check-contracts
python3 scripts/source_content_receipt.py fixture-seal \
  --fixture-root /tmp/source-receipt-fixture \
  --capture /tmp/source-receipt-fixture/capture.json \
  --content /tmp/source-receipt-fixture/body.html \
  --out /tmp/source-receipt-fixture/receipt.json \
  --generated-utc 2026-07-16T13:00:02Z
python3 scripts/source_content_receipt.py validate \
  --fixture-root /tmp/source-receipt-fixture \
  --receipt /tmp/source-receipt-fixture/receipt.json \
  --content /tmp/source-receipt-fixture/body.html
```

The capture contract is strict. It records one canonical HTTPS SEC archive URL, exact retrieval
timestamps/status/content type, and raw byte-range anchor requests. The sealer derives receipt,
origin, body, fragment, excerpt, normalized-value, and anchor hashes. Even a valid anchor proves
only provenance of bytes. `normalized_value_semantically_verified` is always false and semantic
interpretation remains an adjudication task.
