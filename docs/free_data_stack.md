# Zero-Dollar Research Data Stack

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

The broader multi-source orchestration, no-key Nasdaq/Federal Reserve/CFTC adapters, and
pre-adjudication deduplication contract are documented in
[`docs/free_source_ensemble.md`](free_source_ensemble.md). This file retains the provider-specific
procurement and Twelve Data Basic decision record.

Status: paid market-data procurement is paused. Massive and Databento remain disabled. This lane
uses free official/web sources and an optional Twelve Data Basic free account. It cannot activate a
strategy, change a risk limit, create Buy authority, or place or preview an order.

`config/data_procurement_policy.json` is the controlling local spend policy: external data spend is
hard-capped at `$0.00`; paid purchases, subscription activation, free-tier upgrades, agent acceptance
of external terms, and submission of payment information are all prohibited.

## Source Roles

| Source | Cost | Allowed role | Hard limitation |
|---|---:|---|---|
| SEC EDGAR | $0 | Primary filings and issuer-event verification | No prices |
| Company IR / official releases | $0 | Primary catalyst confirmation | Issuer-only evidence must be labeled |
| BLS, Fed, Treasury, BEA, Census | $0 | Official macro releases and revisions | No equity prices |
| Web search/fetch | $0 | Discovery and corroboration | Never establishes a price or verifies a rumor by itself |
| Nasdaq Trader public files | $0 | Current symbols, halts, regulatory lists | Not a survivorship-free historical master |
| Robinhood read-only | $0 | Fixed market anchor plus post-discovery candidate quote/spread/tradability hydration | One source only; candidate bundle is not the market anchor or a second Buy source; no orders |
| Twelve Data Basic | $0 | Shadow current-price check and split-adjusted daily bars | Realtime feed is not consolidated; no NBBO; incomplete free corporate actions/delisted history |
| Alpha Vantage free | $0 | Secondary date-granular daily diagnostic | 25 requests/day; never live-price authority |

## Twelve Data Boundary

The Basic plan currently advertises 8 credits/minute and 800/day, internal non-display use,
real-time U.S. equities/ETFs, and reference data. Twelve Data says its default real-time feed
represents roughly 5% of U.S. trading volume; next-day historical/EOD data covers full U.S. market
volume. Daily/weekly/monthly prices are split-adjusted, while intraday data is unadjusted. The
separate split and dividend endpoints require a paid Grow plan, so this system must not claim a
complete auditable adjustment or corporate-action history from Basic.

Official references checked 2026-07-11:

- https://twelvedata.com/pricing
- https://support.twelvedata.com/en/articles/9935903-us-equities-market-data
- https://support.twelvedata.com/en/articles/5179064-are-the-prices-adjusted
- https://twelvedata.com/terms
- https://www.alphavantage.co/support/
- https://www.sec.gov/about/developer-resources
- https://nasdaqtrader.com/Trader.aspx?id=Help

## Optional Free Account

No credit card or paid plan is requested by the repository. The account owner must personally
create the free account and accept Twelve Data's terms. Never paste the API key into chat. Store it
locally with:

```bash
./scripts/setup_twelve_data_key.sh
python3 scripts/twelve_data_free_probe.py
```

The setup script hides input and writes only to git-ignored `secrets/trading.env` with mode 600.
The probe downloads five AAPL daily bars, normalizes them, emits only hashes/counts, and grants no
authority. The provider remains disabled in `config/data_lake.json` until license review and shadow
quality evidence are complete.

## What Works Without Any Account

The agent ensemble can search the web, read SEC/IR documents, collect BLS data, classify catalysts,
build Watch lists, and issue No-Trade decisions. Without a validated price source, current entry,
liquidity, and spread are unknown, so the constitution requires Watch/No Trade rather than Buy.
Direct web/SEC/IR findings remain lead-only until the control plane revalidates a trusted
production source receipt for the exact target and claim; that registry is currently empty.

## Post-Discovery Candidate Prices

The Morning workflow now converts the persisted catalyst-discovery denominator into one bounded,
hash-bound request and uses an audited collector with only Robinhood quote and tradability reads.
The deterministic parent strips raw payloads, preserves unavailable symbols in the denominator,
re-derives freshness/session/spread state, and seals a self-hashed write-once bundle. This remedies
the earlier fixed-SPY-only coverage gap without giving the research agents general broker access.

Candidate prices are research observations only. They never replace the fixed market anchor, never
count as a second independent Buy source, and never grant Buy or execution authority. See
[`docs/candidate_quote_hydration.md`](candidate_quote_hydration.md) for the artifact, failure, and
freeze contracts.
