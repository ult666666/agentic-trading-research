# SEC Filing Metadata Event Parser

`scripts/sec_event_parser.py` converts a validated SEC EDGAR filings batch into a
cutoff-aware, hash-bound research artifact. It is an isolated diagnostic foundation. It is
not connected to the morning decision path, scoring, Buy authorization, performance metrics,
or execution.

## Input boundary

The parser accepts only `provider_collection_batch/1.0` artifacts whose provider,
authority source, and dataset are exactly `sec_edgar`, `sec_edgar`, and `filings`. The shared
provider validator must pass, every accession must match its record ID, and duplicate
accessions fail closed.

The input contains structured SEC submissions metadata, not filing-document content. The
parser does not make a network request and does not parse HTML, exhibits, issuer prose, or
free-form news. Primary-document descriptions are hashed rather than copied into the output.

## Knowledge cutoff

Both SEC acceptance time and local observation time must be present and no later than the
explicit UTC knowledge cutoff. Every source record appears exactly once in either `filings`
or `exclusions`:

1. missing acceptance timestamp: `acceptance_timestamp_missing`
2. acceptance after cutoff: `accepted_after_knowledge_cutoff`
3. observation after cutoff: `observed_after_knowledge_cutoff`

`complete_input_denominator=true` means only that every record in the validated input batch
was accounted for. It never means the batch covers the complete SEC universe. Collection
errors are counted, hash-bound, and exposed as a limitation.

## Conservative semantics

The versioned mapping lives in `config/sec_event_parser_policy.json` and is constrained by
`config/catalyst_taxonomy.json`.

- Form 8-K items are classified from exact item codes. Unknown valid codes are preserved and
  require document review. Malformed tokens are counted and never inferred.
- Item 9.01 is supplemental and never counted as a standalone event.
- Amendments remain separate filings; no parent accession or changed substance is inferred.
- S-1/S-3/F-1/F-3 are potential financing registration metadata, not proof of a sale.
- 424B forms are potential offering-term metadata, not proof of completion.
- EFFECT is an effectiveness notice, not proof that securities were sold.
- Item 2.02 and 7.01 retain the risk that information may be furnished rather than filed.
- Every filing and item has `filing_content_reviewed=false`, `substance_confirmed=false`,
  `directional_signal=null`, `actionability=false`, and
  `grants_catalyst_confirmation=false`.

The policy is based on the SEC's official EDGAR API and form descriptions:

- <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- <https://www.sec.gov/files/form8-k.pdf>
- <https://www.sec.gov/files/forms-3.pdf>

## Integrity model

The bundle binds the raw source file, source batch self-hash, policy file, taxonomy file,
each source record, each item event, each filing, each exclusion, and the final artifact.
`validate` then rebuilds the expected artifact from those sources and compares canonical
bytes. Recomputing the final hash after a semantic edit is therefore insufficient to pass.

Publication is atomic and write-once. An existing destination is never replaced.

## Commands

```bash
python3 scripts/sec_event_parser.py check

python3 scripts/sec_event_parser.py parse \
  --input /path/to/sec_filings_batch.json \
  --cutoff 2026-07-10T16:00:00Z \
  --out /path/to/sec_filing_events.json

python3 scripts/sec_event_parser.py validate \
  --input /path/to/sec_filing_events.json

python3 scripts/test_sec_event_parser.py
```

The module must remain offline and research-only until a separate, reviewed integration
change explicitly defines how metadata candidates are corroborated against filing documents
and admitted to the existing evidence graph. Metadata alone must never satisfy catalyst
confirmation or weaken any source, risk, freeze, or Buy gate.
