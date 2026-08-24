# Legacy Source-Router Health

## Purpose

`scripts/source_router.py` is a legacy diagnostic router used by older attended prompts. It is not
the governance-v2 source authority, the free-source ensemble, or a Buy gate. Its output may identify
a source-diversity precondition, but it cannot grant price, catalyst, risk, freeze, strategy, broker,
or execution authority.

## Fail-Closed Health Contract

The router reads `data/source_health.json` as an untrusted observation and now requires:

- a timezone-aware `generated_utc` no older than 24 hours;
- no timestamp more than five minutes in the future;
- a connector map;
- `available` to be literal JSON `true`, not a truthy string such as `"partial"`;
- current local presence of required Alpha Vantage or FMP credential fields;
- a closed local circuit breaker.

Credential values are never emitted. A stale, malformed, future-dated, non-boolean, credential-
inconsistent, or circuit-open source is unavailable. If no source remains, the route is degraded
and research confidence must fall; no fallback value is fabricated.

The shipped health file was generated on 2026-06-23 and is intentionally rejected as stale. Its old
claims are historical notes, not current reachability.

## Buy Compatibility Field

For backward compatibility the buy-check response retains `buy_permitted`, but it now also emits
`source_precondition_passed`, `diagnostic_only=true`, `buy_authority_granted=false`, and
`execution_enabled=false`. Even a true source precondition is only necessary, never sufficient.
The deterministic live-price, catalyst, coverage, portfolio-risk, workflow-quality, and freeze
gates remain authoritative.

## Commands

```bash
python3 scripts/source_router.py --category price_history
python3 scripts/source_router.py --buy-check broker_robinhood web_reputable_news
python3 scripts/test_source_router_health.py
```

Models must not refresh `data/source_health.json` merely by assertion. A future replacement should
use deterministic per-connector probes and an ignored, hash-bound runtime receipt.
