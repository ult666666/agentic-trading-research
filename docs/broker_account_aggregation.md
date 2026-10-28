# Broker Account Aggregation

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Current state

There are now two intentionally separate layers:

1. `asset_allocation_context.py` is the production, tighten-only risk context. It combines the
   sanitized Robinhood sizing snapshot with sanitized Webull exposure. Robinhood remains the sole
   manual-ticket sizing provider; Webull can only reduce a candidate size or veto a new Buy.
2. `broker_account_aggregate.py` remains an offline shadow diagnostic for generic normalized
   snapshots. Its Webull namespace is verified and admitted, but the aggregate has no production,
   price, source, Buy, or execution authority.

The Webull collector uses OAuth and exactly five read-only tools: account list, account balance,
account positions, open orders, and order history. The last is a bounded 100-day fallback for a
failed open-orders query. All mutation, preview, cancellation, watchlist-write, quote, and market
data tools are excluded. Raw responses, account identifiers, order identifiers, and model
transcripts are deleted; only sealed identifier-free snapshots and receipts persist.

As of the 2026-07-16 live audit, Account Infos access can enumerate two cash accounts and read
their balances and positions. Order Query is also authorized: the primary open-orders tool was
intermittent, while the bounded order-history fallback completed for both accounts. One account's
missing net-liquidation field is represented by a conservative market-value lower bound, which can
only tighten risk. A fresh Robinhood collection plus this Webull receipt produced a
`ready_for_tighten_only` context and passed source-hash revalidation.

## Contracts

- Policy: `config/broker_aggregation.json`
- Input: `schemas/sanitized_broker_snapshot.schema.json`
- Output: `schemas/broker_account_aggregate.schema.json`
- Builder: `scripts/broker_account_aggregate.py`
- Adversarial tests: `scripts/test_broker_account_aggregate.py`

The input contract excludes account identifiers, credentials, names, email addresses, raw MCP
payloads, margin, shorts, options, futures, crypto, event contracts, and non-USD accounts. Each
snapshot is self-hashed and must cite a valid read-only broker-audit receipt hash.

The output has two intentionally different account views:

1. `portfolio_totals` combines admitted providers for risk context only.
2. `manual_execution_sizing_basis` uses only `robinhood_readonly`. Webull or IBKR cash is never
   treated as Robinhood buying power, and the aggregate never authorizes a share count.

Quotes remain as per-provider observations. The system reports disagreement in basis points and
always emits `synthetic_consolidated_price: null`; it does not average conflicting values into a
new price or grant live-price authority.

## Remaining activation check

1. Keep **Account Infos** and **Order Query** authorized for the intended accounts. Order placement
   is not used by this project.
2. Run the collector and require `balances_complete=true`, `positions_complete=true`, and
   `open_orders_complete=true` in the sealed receipt. A missing component is never treated as zero.
3. Run the allocation builder with a fresh same-session Robinhood snapshot. Its status must be
   `ready`; hashes and source timestamps are rechecked again before report freeze.
4. Observe at least five natural runs and review partial failures, duplicate symbols, account-type
   changes, corporate actions, and order-query fallback use.
5. Any future market-data authority proposal is a separate source-governance change. Account
   aggregation never grants quote, Buy, preview, mutation, or execution authority.

Live order placement, preview, simulation, modification, and cancellation remain permanently out
of scope for this project. The user manually enters any proposed ticket in Robinhood.

## Commands

```bash
python3 scripts/broker_account_aggregate.py --check-policy
./scripts/run_webull_readonly_collector.sh --target 2026-07-16
python3 scripts/asset_allocation_context.py validate \
  --input data/runtime/asset_allocation/2026-07-16/context.json
python3 scripts/broker_account_aggregate.py validate-snapshot \
  --target 2026-07-15 --input /path/to/sanitized-snapshot.json
python3 scripts/broker_account_aggregate.py aggregate \
  --target 2026-07-15 \
  --input /path/to/robinhood-sanitized.json \
  --input /path/to/webull-sanitized.json \
  --out /path/to/broker-account-aggregate.json
```

## Webull OpenAPI boundary

Webull's official OpenAPI documents separate trading/account APIs from market-data APIs. OpenAPI
uses application credentials and request signing; market-data access may require a separate
non-display subscription from subscriptions in the Webull app. Connecting an account in Codex
does not prove those OpenAPI entitlements exist, and this layer neither requests nor stores those
credentials.

Primary references:

- <https://developer.webull.com/apis/docs/>
- <https://developer.webull.com/apis/docs/authentication/overview/>
- <https://developer.webull.com/apis/docs/trade-api/account/>
- <https://developer.webull.com/apis/docs/reference/account-position/>
- <https://developer.webull.com/apis/docs/market-data-api/overview/>
