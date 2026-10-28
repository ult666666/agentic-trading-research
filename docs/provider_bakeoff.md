# Shadow Provider Bake-off

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: the deterministic 30-session Massive-versus-Databento evaluation harness is implemented.
Both commercial providers remain disabled, no paid request has been made, the tracked license
matrix is pending, and no score or role recommendation exists from real data.

Authority: `AGENTS.md` overrides this document. The harness is a data-procurement diagnostic. It
cannot enable a provider, grant source authority, feed production scoring, activate a strategy,
change risk limits, preview an order, or execute anything.

## Control Flow

```text
disabled shadow adapter / independent fixture evaluator
                    |
                    v
provider_bakeoff_observation/1.0 (aggregate counts + hashes only)
                    |
                    v
provider_bakeoff.py daily
  schema/hash -> PIT cutoff -> correction -> fault -> license -> cost -> quality
                    |
                    v
provider_quality_receipt/1.0
                    |
                    v
provider_bakeoff.py rollup
  30 distinct sessions + all fixtures + both providers + written rights
                    |
                    v
provider_bakeoff_scorecard/1.0
  collecting | blocked | hard_gate_failed | quality_failed |
  candidate_for_governance_review
```

The 05:00 `run_data_intake.sh` invokes `daily` and `rollup` after the independent point-in-time
lake receipt. This call performs no network collection. Missing observations produce `collecting`;
the pending license template keeps a complete but unattested campaign `blocked`. Structural
bake-off failure is logged and cannot alter data-lake readiness or Morning authority.

## Files and Storage

- `config/provider_bakeoff.json`: campaign duration, provider pair, hard thresholds, weights,
  cost caps, required fault matrix, paths, and immutable no-authority safety fields.
- `config/provider_license_matrix.template.json`: hash-valid pending template. It is not approval.
- `scripts/provider_bakeoff.py`: strict validator, daily evaluator, and deterministic rollup.
- `schemas/provider_bakeoff_observation.schema.json`: adapter-to-evaluator aggregate contract.
- `schemas/provider_quality_receipt.schema.json`: daily receipt contract.
- `schemas/provider_bakeoff_scorecard.schema.json`: final rollup contract.
- `schemas/provider_license_matrix.schema.json`: written-rights attestation contract.
- `scripts/test_provider_bakeoff.py`: adversarial and full 30-session fixture tests.

Runtime artifacts are ignored:

```text
data/runtime/provider_bakeoff/
  observations/<target>/<provider>.json
  receipts/<target>.json
  rollup/current.json
  license_matrix.json
```

Only aggregate fixture counts, source/fixture hashes, return differences in basis points, fault
outcomes, cost totals, and license-document hashes may persist. Raw vendor responses, credentials,
account data, signed contract contents, market prices, and symbol-level return values are excluded.

## License Gate

An attestation is approved only when it has a document hash, independent review-record hash,
review timestamp, future expiry date, and explicit rights for normalized retention, derived
features, internal backtesting, internal model research, correction history, and delisted history.
The observation must bind the same attestation hash.

Store the actual matrix only at the ignored runtime path. Do not put contract text, names,
signatures, or vendor credentials in the repository. Even an approved matrix does not enable the
adapter: `config/data_lake.json` independently requires both providers to remain disabled and
`authority_enabled=false`.

## Hard Gates

Any of these prevents a candidate scorecard:

- information included after its knowledge cutoff;
- a changed provider value without a correction receipt;
- unsafe handling of 429, 5xx, hang, truncation, out-of-order, stale, or schema-change faults;
- provider or total campaign cost above the configured cap;
- invalid observation, daily receipt, hash, campaign identity, or attestation binding;
- missing or rejected written license rights.

The complete campaign also requires 30 distinct observations per provider, every metric family,
at least one cutoff fixture, correction fixture, bitwise as-of replay, every required fault,
morning-availability observations, and cross-provider return comparisons. An absent denominator is
`unobserved`, never 0% or 100%, and no weighted score is emitted for incomplete evidence.

## Quality and Scorecard

The hard thresholds mirror the research plan: listing and symbol-interval accuracy, delisted and
corporate-action coverage, daily/minute coverage, adjustment round trips, quote/trade ordering,
as-of determinism, safe outages, morning availability, and cross-provider return differences.
Hard gates run before the weighted score.

The score uses the fixed 100-point weights in the campaign config. A strictly higher qualifying
score may identify a `candidate_primary_historical` and a
`candidate_independent_validator`. A tie fails quality rather than selecting alphabetically. The
strongest terminal is `candidate_for_governance_review`; `automatic_activation=false` remains
explicit and a separate protected source-authority change is required.

## Vendor Semantics to Test

Massive documents both adjusted and unadjusted REST aggregate views and states that stock flat
files are unadjusted. Its stock flat files are finalized around 11:00 ET on the next day, so that
path alone cannot satisfy the project's 05:15 PT prior-session-data objective. The campaign must
measure the appropriate REST route or classify flat files as backfill only:
https://massive.com/docs/rest/stocks and
https://massive.com/docs/flat-files/stocks/overview.

Databento documents event-time symbol preservation, symbol reuse, multiple market timestamps,
point-in-time corporate-action records, and historical request cost estimation. The campaign must
test these semantics against independent fixtures and recorded invoices rather than treating
documentation as observed quality:
https://databento.com/docs/standards-and-conventions/symbology,
https://databento.com/docs/schemas-and-data-formats/corporate-actions, and
https://databento.com/docs/api-reference-historical/basics/encodings.

## Commands

Validate configuration, pending license template, and disabled-provider boundary:

```bash
python3 scripts/provider_bakeoff.py check
```

Evaluate one target after shadow adapters have produced aggregate observations:

```bash
python3 scripts/provider_bakeoff.py daily --target YYYY-MM-DD
```

Rebuild the campaign scorecard from hash-valid daily receipts:

```bash
python3 scripts/provider_bakeoff.py rollup
```

## Promotion and Rollback

Provider purchase, network adapter activation, credential provisioning, and any source-authority
change are separate approvals. A passing scorecard supplies evidence for that review but cannot
perform it. Rollback removes the two fail-soft calls from `run_data_intake.sh` and the isolated
config/schema/script/test/docs files; existing data-lake, strategy, risk, report, broker, and
execution behavior remains unchanged.
