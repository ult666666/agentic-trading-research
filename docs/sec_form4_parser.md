# SEC Form 4 XML Normalizer

`scripts/sec_form4_parser.py` is an offline deterministic boundary for already-retrieved SEC
Ownership XML. It converts a Form 4 or Form 4/A document into a bounded, hash-valid transaction
bundle without fetching the filing, invoking a model or broker, retaining owner PII, or granting
research or financial authority.

## Authority and source semantics

The policy is based on the SEC's current [Ownership XML technical-specification index](https://www.sec.gov/submit-filings/technical-specifications),
[Ownership transaction-code table](https://www.sec.gov/edgar/searchedgar/ownershipformcodes.html),
and [Section 16 interpretation 233.03](https://www.sec.gov/rules-regulations/staff-guidance/corporation-finance-interpretations/exchange-act-section-16-related-rules-forms).

The central non-inference rule is deliberate: transaction code `P` means an open-market **or
private** purchase. The parser can identify a structurally valid non-derivative `P/A` row for later
review, but it always emits:

```text
open_market_confirmed=false
private_purchase_excluded=false
grants_insider_purchase_confirmation=false
```

The SEC also states that a `P` purchase should not carry disposition code `D`. The parser preserves
such a filed row as `conflicted`, adds `transaction_code_direction_conflict`, and excludes it from
the structured-purchase denominator instead of silently repairing or discarding it.

## Inputs

The parser receives two local files:

1. the raw SEC Archive Ownership XML file;
2. a `sec_form4_parse_request/1.0` containing accession, issuer CIK, symbol, accepted time,
   retrieval time, knowledge cutoff, exact SEC Archive URL, and the XML content hash.

The request must prove `accepted <= retrieved <= knowledge cutoff`, and generation cannot precede
the cutoff. The URL must exactly match the request CIK/accession and a flat `.xml` file under
`https://www.sec.gov/Archives/edgar/data/`. Redirects, query strings, fragments, credentials, other
domains, and path traversal are impossible in this offline stage.

## Parsing and privacy

The XML parser is bounded by byte, element, depth, owner, transaction, text, and footnote-reference
limits. DTDs and entities are refused before parsing. It supports XML namespaces by local element
name and preserves:

- Form 4 versus Form 4/A and original-submission date;
- period of report and filing-level Rule 10b5-1 checkbox;
- issuer CIK and trading symbol;
- hashed reporting-owner identity plus boolean role flags;
- non-derivative and derivative transaction rows;
- transaction date/code, acquisition/disposition, shares, reported price, reported notional,
  post-transaction shares, direct/indirect ownership, derivative terms, and footnote-reference
  count;
- explicit valid, incomplete, or conflicted semantic status and machine-readable risk flags.

Owner names, addresses, titles, signature text, owner CIKs, footnote text, and raw XML are not copied
to the bundle. A stable hash of each reporting-owner CIK supports later deduplication without
persisting the identifier. The filing-level 10b5-1 checkbox does not identify which transaction is
covered, so every transaction retains `form_10b5_1_scope_unknown=true` when that box is checked.

Missing numeric fields are `null`, never zero. Reported notional is a unit-unspecified arithmetic
product of filed shares and price; it is not labeled USD, market value, profit, or materiality.

## Output and validation

`sec_form4_transaction_bundle/1.0` is write-once and self-hashed. Validation checks transaction
hashes, immutable non-authority fields, and then rebuilds the entire artifact from the original
request, XML, policy, and generation timestamp. Recomputing only the outer hash after changing a
classification or candidate flag cannot pass.

```bash
python3 scripts/sec_form4_parser.py check

python3 scripts/sec_form4_parser.py parse \
  --request /path/to/form4_request.json \
  --xml /path/to/form4.xml \
  --out /path/to/form4_bundle.json

python3 scripts/sec_form4_parser.py validate \
  --input /path/to/form4_bundle.json

python3 scripts/test_sec_form4_parser.py
```

## Integration boundary

This component is not connected to production retrieval, the evidence graph, scoring, the Buy
gate, freeze, metrics, or execution. A later reviewed integration may use it as deterministic input
to a Form 4 document-review task. That task must still resolve footnotes, distinguish open-market
from private purchases, assess clustering and materiality, preserve sales as non-symmetric
context, seek independent catalyst evidence, and pass evidence adjudication plus risk review.

No normalized row alone confirms insider-buying alpha or future profitability.
