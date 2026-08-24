# FMP Starter Integration

Status: active, research-only, local personal use  
Verified: 2026-07-16  
Execution authority: none

## Purpose

The paid FMP key has two separate jobs:

1. Provide the deterministic live-price primary used by the morning preflight.
2. Provide bounded prior-close history and earnings-calendar discovery context before any model
   process starts.
3. Provide normalized target-session EOD marks to the post-close review without depending on an
   interactive connector permission flow.
4. Supply positive-volume context alongside the extended-hours trade price without replacing the
   extended-hours price authority.

The second job is implemented by `scripts/fmp_paid_enrichment.py`. It converts a large provider
response into one compact, self-hashed local artifact under
`data/runtime/fmp_enrichment/<target>.json`. Raw responses and credentials are not retained.

## Current Entitlement

The official FMP pricing page describes Starter as real-time US coverage, 300 calls per minute,
up to five years of historical data, market news, and a trailing-30-day 20 GB bandwidth limit.
Premium-only intraday charts and Ultimate-only bulk/batch delivery are not assumed.

Official references:

- https://site.financialmodelingprep.com/developer/docs/pricing
- https://site.financialmodelingprep.com/developer/docs
- https://site.financialmodelingprep.com/developer/docs/cycle-times-stable
- https://site.financialmodelingprep.com/developer/docs/terms-of-service

Live, secret-safe probes on 2026-07-15 established the following account-specific contract:

| Endpoint | Result | Production use |
|---|---:|---|
| `/stable/quote` | HTTP 200 | Current regular-session quote |
| `/stable/aftermarket-trade` | HTTP 200 | Timestamped pre/post-market trade |
| `/stable/aftermarket-quote` | HTTP 200 | Bid/ask context; never midpoint-as-price |
| `/stable/news/stock` | HTTP 200 | Discovery only |
| `/stable/historical-price-eod/full` | HTTP 200 | Completed daily technical context |
| `/stable/earnings-calendar` | HTTP 200 | Event-date discovery only |
| `/stable/historical-price-eod/non-split-adjusted` | HTTP 200 | Entitlement audit only |
| `/stable/historical-price-eod/dividend-adjusted` | HTTP 200 | Entitlement audit only |
| `/stable/dividends` | HTTP 200 | Entitlement audit only |
| `/stable/splits` | HTTP 200 | Entitlement audit only |
| `/stable/delisted-companies` | HTTP 200 | Entitlement audit only |
| `/stable/batch-quote` | HTTP 402 | Disabled |
| `/stable/batch-aftermarket-trade` | HTTP 402 | Disabled |
| `/stable/batch-aftermarket-quote` | HTTP 402 | Disabled |
| `/stable/news/press-releases` | HTTP 402 | Disabled |

The runtime never infers entitlement from the marketing page. HTTP status, payload shape, and
provider market timestamps remain decisive.

## Morning Flow

1. At 04:56 PT, the continuity service runs `scripts/fmp_connection_check.py` with credentials
   loaded only for the bounded deterministic probe. It requires authenticated SPY/QQQ/IWM/DIA
   quotes, positive volume, and extended-hours trade/quote transport, then clears all credentials
   before readiness/model diagnostics.
2. `scripts/data_preflight.py` obtains fresh single-symbol index/equity quotes and targeted news.
3. `scripts/fmp_paid_enrichment.py` spends at most 30 calls and 75 wall-clock seconds on one
   earnings-calendar request and up to 28 single-symbol history requests.
4. It rejects every bar whose date is on or after the target session, preventing an incomplete
   target-day bar from entering prior-close technical context.
5. It computes MA20/50/200, ATR14, 20-session annualized log volatility, average volume, 5/20-day
   returns, 20/50-day ranges, and the latest completed-session gap.
6. It filters the full earnings calendar to tracked symbols and persists only normalized dates and
   numeric estimate/actual fields. Event time remains unavailable.
7. `run_morning_scan.sh` clears the complete provider/SEC credential allowlist before any model
   process. `audited_claude.py` independently strips the same names and disables dotenv fallback
   for every Claude child; the Bash hook rejects direct credential-file/loader access.
   Models receive only the artifact path and status.

The live-price snapshot remains authoritative. The enrichment artifact cannot rescue stale live
data, verify a catalyst, satisfy a Buy gate, alter a risk limit, or count as an independent source.

## Post-Close Flow

At 13:15 PT, `scripts/post_close_market_snapshot.py` reads the benchmark, watchlist, and frozen
candidate symbol set, capped at 20 unique symbols. It makes one bounded
`/stable/historical-price-eod/full` request per symbol for the target date and accepts a mark only
when exactly one sane target-date OHLCV row exists after the validated session close. The compact,
self-hashed artifact is written to
`data/runtime/post_close_market_snapshot/<target>.json` before any model starts.

The artifact distinguishes `complete`, `partial`, `unavailable`, `before_close`, `non_trading_day`,
`invalid_run_id`, and `key_missing`. It stores no raw response and no credential. Adjustment
semantics remain `provider_unspecified`, so these marks are closing observations only and do not
independently enter total-return labels, live-price authority, Buy authorization, risk, or execution.

`scripts/post_close_report_contract.py` then binds the final narrative and
`data/watchlist.json._current_data_status` to the newest preflight available before finalization and
to the exact closing-snapshot hash. A fresh Markdown file alone is no longer a successful
post-close terminal.

## Efficiency

The live validation for target `2026-07-16` used 27 of the 30 enrichment-call budget:

- 26/26 requested symbols had sufficient history.
- Every accepted history series ended on `2026-07-15`, before the target session.
- The provider returned 3,069 earnings-calendar rows; only 11 tracked events were retained.
- No raw bars or raw calendar rows were persisted.
- The resulting artifact completed in 22.4 seconds, had zero warnings, and passed both its
  self-hash and semantic validators.

This gives the agents deterministic daily context without spending tokens parsing thousands of
provider rows.

## Safety And Licensing

- Personal-use data remains local and private. Do not publish, redistribute, expose through the
  operator console, or share the key or FMP-derived values with third parties.
- The retention policy is `normalized_metrics_and_event_dates_only`; raw provider payloads are
  discarded after normalization.
- Historical adjustment semantics are conservatively labeled provider-unspecified. These metrics
  are not eligible for quant total-return labels.
- FMP news and earnings rows are locators/discovery leads. SEC filings and issuer IR remain the
  primary verification authorities.
- `config/fmp_data_rights.json` currently prohibits raw-payload persistence, normalized bulk
  history persistence, a long-lived derived corpus, a survivorship-free backtest corpus, and
  retention after subscription. `scripts/fmp_dataset_entitlement.py` proves endpoint transport but
  cannot override these rights. Persistent FMP-backed quant storage remains disabled until written
  provider permission is recorded and independently reviewed.
- Missing, stale, malformed, future-dated, rate-limited, or entitlement-blocked data remains an
  explicit warning or unavailable field. It is never interpolated.

## Operations

```bash
python3 scripts/fmp_connection_check.py --print
python3 scripts/fmp_dataset_entitlement.py --print
python3 scripts/fmp_paid_enrichment.py --check
python3 scripts/fmp_paid_enrichment.py --target YYYY-MM-DD
python3 scripts/fmp_paid_enrichment.py --validate data/runtime/fmp_enrichment/YYYY-MM-DD.json
python3 scripts/post_close_market_snapshot.py --target YYYY-MM-DD --run-id <postclose-run-id>
python3 scripts/post_close_market_snapshot.py --validate \
  data/runtime/post_close_market_snapshot/YYYY-MM-DD.json
```

Do not increase request budgets or enable disabled endpoints from marketing claims alone. Re-probe
the exact endpoint after a plan change, then update the config, tests, manifest, journal, and
release snapshot as one reviewed unit.
