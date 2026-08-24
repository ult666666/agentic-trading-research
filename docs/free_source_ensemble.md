# Zero-Dollar Heterogeneous Source Ensemble

## Purpose

This layer combines complementary free/public sources without pretending that more feeds
automatically create better evidence. Deterministic collectors gather and normalize data; agents
discover, challenge, and synthesize; deterministic validators decide what the evidence may support.

It is a research system. It does not place, preview, simulate, modify, or cancel orders. It cannot
activate a strategy, alter risk limits, grant Buy authority, or guarantee profit.

## Architecture

```text
Official/public APIs       Optional free-key feeds       Read-only broker       Web/IR
SEC, BLS, Nasdaq, Fed,     FMP, Alpha Vantage,           sanitized Robinhood    official pages,
CFTC                       Twelve Data                   artifacts              discovery
          \                       |                            |                    /
           +---------------- deterministic collection and normalization --------+
                                      |
                         immutable point-in-time data lake
                                      |
                    free_source_ensemble plan (assigns consumers)
                                      |
          market / catalyst / SEC / macro / technical / risk / skeptic agents
                                      |
                   free_source_fusion (hash + provenance receipt)
                                      |
                       existing evidence-adjudicator + gates
                                      |
                 Watch / No-Trade / conditional manual proposal only
```

The important separation is:

- **Hands:** bounded HTTP collectors, local archive readers, sanitized read-only broker collector,
  and web research agents. They acquire and normalize data.
- **Brain:** specialist agents compare evidence, find catalysts, test counterarguments, interpret
  regime context, and write structured research artifacts.
- **Spinal cord:** source matrix, evidence schema, risk gate, research authority, freeze gate, and
  append-only ledgers. These deterministic components can veto model prose.

## Active Zero-Key Sources

| Source | Lane | What it contributes | Hard limitation |
|---|---|---|---|
| SEC EDGAR | filings/catalysts | filing metadata and primary-document provenance | requires configured SEC identity; no price |
| BLS public API | macro | official current-published labor/inflation observations | no guaranteed release timestamp in normalized records |
| BLS release calendar | macro schedule | official scheduled Eastern Time release events and revisions | schedule only; does not confirm actual publication |
| Nasdaq Trader directory | identity | current symbol, listing, ETF/test/status metadata | current snapshot only; not survivorship-free history |
| Nasdaq Trader adds/deletes | listing events | daily system add/delete observations accumulated into a shadow rollforward | current event file only; unknown starts remain unknown |
| Nasdaq Trader ex-date notices | corporate-action notices | special/pending ex-date exceptions for agent follow-up | no action type, amount, or complete adjustment coverage |
| Federal Reserve H.15 DDP | macro/rates | current-published policy/rate context | no pre-revision vintage; excluded from release-time modeling |
| CFTC COT public API | regime context | weekly positioning context across equity, rate, energy, and metal contracts | lagged weekly context; futures remain prohibited instruments |
| Local point-in-time archive | daily history | validated historical bars available before each cutoff | never a live quote |

Official implementation references:

- SEC EDGAR APIs: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- BLS release calendar: <https://www.bls.gov/schedule/news_release/bls.ics>
- Nasdaq symbol definitions: <https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs>
- Nasdaq symbol lookup and current event files: <https://nasdaqtrader.com/Trader.aspx?id=symbollookup>
- Federal Reserve DDP help: <https://www.federalreserve.gov/datadownload/help/>
- CFTC COT overview: <https://www.cftc.gov/MarketReports/CommitmentsofTraders/AbouttheCOTReports/index.htm>
- CFTC Legacy Futures-Only dataset: <https://publicreporting.cftc.gov/Commitments-of-Traders/Legacy-Futures-Only/6dca-aqww>

## Optional Free Sources

FMP Basic, Alpha Vantage, and Twelve Data remain optional. Their absence does not block the public
source lanes. A plan records only whether required credential fields are present; it never writes
credentials, cookies, account identifiers, or raw responses.

Free account registration, CAPTCHA, email verification, and acceptance of external terms remain
manual acts. Agents cannot accept terms on the user's behalf. The procurement ceiling is enforced
by `config/data_procurement_policy.json`: external data spend is exactly `$0.00`.

## Daily Flow

At 05:00 PT, `scripts/run_data_intake.sh`:

1. validates the trading day, integrity manifest, broker read-only boundary, and SEC identity;
2. writes `data/runtime/free_source_ensemble/<target>/plan.json`;
3. collects enabled public/local sources into immutable normalized batches;
4. writes `data/runtime/data_lake/readiness/<target>.json` and a cutoff snapshot;
5. derives `data/runtime/security_master_rollforward/current.json` as a shadow diagnostic;
6. atomically refreshes `data/runtime/free_source_health/current.json` from hash-linked
   cross-session receipts and redacted plans;
7. records missing optional feeds as degradation, never fabricated success.

The rollforward deduplicates repeated observations of Nasdaq add/delete events, builds listing
episodes, keeps unknown starts as `null`, and preserves reconciliation conflicts. It cannot certify
complete history or supply eligibility, scoring, strategy activation, or execution. A separately
reviewed historical seed and license attestation would still be required. Ex-date notices remain a
separate dataset and never count as `corporate_actions`.

The morning ensemble reads the canonical plan, intake receipt, and snapshot. The
`evidence-adjudicator` may write hash-only provenance references to
`data/runtime/research/<target>/free_source_evidence.json` and run:

```bash
python3 scripts/free_source_fusion.py \
  --input data/runtime/research/<target>/free_source_evidence.json \
  --out data/runtime/research/<target>/free_source_fusion.json
```

The fusion receipt:

- deduplicates copies sharing one `origin_id`;
- rejects web/search price claims;
- rejects out-of-lane, rumored, and stale claims;
- preserves same-origin and cross-origin disagreements;
- records only hashes, IDs, timestamps, and booleans;
- can reach only `verified_for_adjudication`, never an evidence/Buy/freeze gate pass.

The cross-session health receipt keeps public failures, missing SEC identity, and an accumulating
local archive separate instead of collapsing all three into one provider-up/down flag. It requires
five valid sessions before a `healthy` rate label is possible and is exposed to morning/post-close
agents as aggregate observability only. See
[`docs/free_source_health.md`](free_source_health.md).

## Source-Diversity Rules

1. More URLs do not imply more independent evidence.
2. A syndicated copy keeps the primary release's origin fingerprint and counts once.
3. An issuer release may establish a primary fact but does not independently corroborate itself.
4. Search snippets never establish a price and never override a sanitized broker quote.
5. Current-published macro data is not a point-in-time vintage unless a release timestamp and
   revision history are explicitly available.
6. COT positioning is context, not a standalone timing signal or a permitted instrument.
7. Conflicts are preserved and adjudicated; values are never averaged to hide disagreement.
8. Missing, stale, malformed, or conflicting material data keeps the candidate at Watch/No-Trade.
9. Listing add/delete observations may trigger verification work but never establish a complete
   security-master interval by themselves.
10. An ex-date exception notice never establishes action type, cash amount, adjustment factor, or
    complete corporate-action coverage.
11. A BLS calendar event establishes scheduled event risk only. It cannot confirm actual release,
    backfill an observation's publication timestamp, or make a release-time study eligible.

## Operational Commands

```bash
# Validate config plus the zero-dollar policy
python3 scripts/free_source_ensemble.py --check

# Build a credential-redacted assignment plan
python3 scripts/free_source_ensemble.py --target YYYY-MM-DD --out /tmp/plan.json

# Validate collection, planning, and fusion contracts
python3 scripts/test_free_source_adapters.py
python3 scripts/test_free_source_ensemble.py
python3 scripts/test_free_source_fusion.py
python3 scripts/free_source_health.py --check
python3 scripts/test_free_source_health.py
python3 scripts/test_nasdaq_event_adapters.py
python3 scripts/test_bls_release_calendar.py
python3 scripts/security_master_rollforward.py --check
python3 scripts/test_security_master_rollforward.py
```

## What This Improves

- Lower single-provider dependency through independent identity, filing, macro, history, broker,
  and web-discovery lanes.
- Better failure isolation: one unavailable provider degrades its lane without erasing unrelated
  evidence.
- Better provenance: every normalized record has separate observation/publication/effective clocks
  where available and a content hash.
- Better agent efficiency: specialists receive assigned source lanes instead of repeatedly searching
  the same material.
- Better epistemic discipline: duplicated stories, stale data, and conflicting values cannot silently
  increase confidence.

This improves research reliability, not assured profitability. Profitability still requires a
validated edge after costs, out-of-sample evidence, conservative sizing, and human execution.
