# Source-receipt denominator manifest (shadow-only)

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

`scripts/source_receipt_manifest.py` inventories the complete local SEC and reputable-news
receipt surface for one target session and one audited run. Its purpose is to make missing,
unsupported, malformed, non-allowlisted, duplicated, and orphaned source units visible instead of
letting a collector summary imply complete coverage.

This component grants no source or catalyst authority. It is deliberately disconnected from
evidence quorum, research classification, Buy authorization, freeze/publication, metrics, broker
access, and execution. The production source-validator registry remains empty.

## Denominators

The manifest derives its denominators from the discovery inputs, not from the collectors' success
lists:

- SEC: every `sec_candidate_seed.candidates[*].recent_filings[*]` value, including malformed or
  unsupported entries;
- news: every `preflight.catalysts.items[*]` value, including missing URLs and publishers outside
  the fixed allowlist.

It then inventories every entry in both receipt directories. Expected receipts are mapped back to
their source-order units; extra receipt files are recorded as orphans; directories, special files,
and unexpected regular files are preserved as explicit anomalies. Each unit records three
separate verdicts: `receipt_file_present`, `receipt_contract_valid`, and
`receipt_unit_identity_matched`. A zero-unit lane can be complete, but a nonzero lane is complete
only when all three verdicts are true for every unit and no orphan or unexpected entry exists.
If a filename matches a receipt lane but its JSON is malformed, non-finite, or duplicate-keyed,
the raw file is still inventoried as present with `json_parse_valid: false`; contract and identity
are false and coverage is incomplete. An unreadable, unstable, non-regular, hard-linked, or
over-limit input still aborts the build because its bytes cannot be safely inventoried.

Contract validation deliberately reuses the existing lane validators'
`_load_verified_receipt` contracts:

- SEC: `scripts/sec_catalyst_receipt_validator.py` required fields, schema, and self-hash;
- news: `scripts/news_corroboration_validator.py` required fields, schema, and self-hash.

The stable manifest bytes are copied exactly into a private `0700` temporary directory as a `0600`
file. The path-only validator loader reads that copy; a post-loader stable read must remain
byte-identical, and the returned document must equal the manifest's parsed document. This prevents
the validator from approving one receipt byte sequence while the manifest hashes another. The
receipt target must also match the manifest target. Identity matching is then independent: SEC
requires the exact accession, canonical symbol, and canonical form from the seed; news requires
the derived
receipt ID, exact URL, canonical symbol, and publisher domain (including the validator's
host-to-allowlist derivation) from the preflight lead. Thus changing any identity field and
re-sealing the inner hash remains incomplete. The policy allowlist must equal the news validator's
versioned domain set. These calls reuse only the receipt-loading contract, not the registered
claim-level `validate()` grant, and therefore create no source authority.

## Integrity boundary

Each manifest binds:

- target session, audited run ID, generation time, and deterministic output path;
- exact bytes of preflight, SEC seed, and both collection summaries;
- exact bytes of the policy, schema, builders, collectors, validators, source matrix, access
  policy, and market calendar;
- exact receipt bytes, inner self-hash verdict, expected schema, target match, reused validator
  contract verdict, and unit-identity verdict;
- full source-order denominators, receipt inventory, orphan list, anomaly list, totals, and a
  canonical manifest hash.

Inputs are opened through no-follow directory traversal, must be stable regular single-link
files, and are bounded by policy. JSON duplicate keys and non-finite numbers are rejected. The
output path is
`data/runtime/research/<target>/source_receipt_manifests/<sha256(audited-run-id)>.json`; it is
created once with mode `0600`. Publication first writes and fsyncs a same-directory private
temporary file, re-reads and fully revalidates it, then uses an atomic hard-link no-replace commit.
The temporary link is removed and the directory is fsynced, leaving one link to a complete final
inode. A partial write, staged-validation failure, or collision cannot put truncated or newly
invalid bytes at the final name. A normal existing single-link final makes `build` fail; it is
never described as an idempotent publication. Use the separate `validate` command to check it.

There is one narrowly controlled crash recovery. If a process dies after the atomic link but
before temporary-link removal, a later `build` may return `recovered_interrupted_commit` only when
the final and exactly one correctly named controlled temporary are both `0600`, are the same inode
with exactly two links, contain identical canonical bytes, and the embedded target/run/path/time
passes full semantic rederivation against current inputs. Recovery then unlinks only that proven
temporary, fsyncs the directory, confirms the final has one link, and validates it again. This is
not an idempotent existing publish. Missing partners, orphan temporaries, multiple links or names,
wrong inode/mode/bytes, unstable reads, or semantic drift are left untouched and fail closed with
`interrupted_commit_requires_quarantine`; an operator must quarantine/reconcile them.

Published bytes must equal canonical JSON plus exactly one LF byte; whitespace or key-order drift
fails even when the parsed object is unchanged. Decision inputs and summaries reject duplicate
keys, JSON constants, and floating-point literals such as `1e9999` that would become non-finite;
matching receipt files preserve the raw diagnostic but cannot pass their contract. Validation
semantically
rebuilds the whole manifest from the currently bound inputs. Any input, control, receipt,
directory inventory, target, run, file-mode, byte-canonicality, or self-hash drift fails closed.

## Commands

```bash
python3 scripts/source_receipt_manifest.py check-policy

python3 scripts/source_receipt_manifest.py build \
  --target YYYY-MM-DD \
  --audited-run-id '<exact-audited-run-id>'

python3 scripts/source_receipt_manifest.py validate \
  --target YYYY-MM-DD \
  --audited-run-id '<exact-audited-run-id>'
```

`--generated-utc` exists only for deterministic controlled use; production integration, if ever
approved, must supply the exact same-run control-plane timestamp and call this builder outside the
model child.

## What remains before production use

This manifest proves local byte-set completeness and receipt/unit consistency, not trustworthy
retrieval or claim semantics. The reused loader contracts do not prove that a receipt's content
was actually fetched from the stated origin, that an anchor supports a claim, or that a catalyst
is true or material.
The current SEC/news collectors are not yet an immutable, same-run, control-plane production
receipt bridge. Before any source authority can be considered, a separately reviewed design must
provide authenticated retrieval provenance, create-once collector outputs, exact claim-to-byte
anchors, independent source-role enforcement, and registered deterministic revalidation. That
future bridge must be activated explicitly; this shadow manifest cannot activate itself.
