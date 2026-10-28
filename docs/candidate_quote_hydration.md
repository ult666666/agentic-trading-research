# Candidate Quote Hydration

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: production-wired, offline-validated, research-only. This component may read candidate
equity quotes from Robinhood through an audited restricted collector. It cannot place, preview,
simulate, modify, replace, or cancel an order. It does NOT verify candidate tradability — see
"Candidate tradability is NOT verified in this lane" below.

## Problem Solved

The pre-discovery Robinhood snapshot uses a fixed market anchor such as SPY. It cannot know which
stocks the catalyst-discovery agent will find later. Candidate quote hydration closes that gap
without expanding broker permissions:

```text
catalyst discovery artifact
  -> deterministic bounded request
  -> restricted quote reads (tradability is not obtainable here; see below)
  -> ephemeral raw response
  -> deterministic sanitizer and validator
  -> write-once candidate quote bundle
  -> evidence agents, freeze receipt, and aggregate health
```

The component runs after discovery and before candidate-specific evidence agents. It never changes
the separate market-anchor or account snapshot path.

## Authority Boundary

A validated row may support only a candidate-specific research statement such as the observed
price, spread, tradability, source timestamp, and session. It is:

- not the singular market anchor;
- not a second independent live source for Buy;
- not evidence that a catalyst is true;
- not evidence of expected return or profitability;
- not account executability, Buy authorization, or execution authority.

Missing, stale, conflicting, malformed, future-dated, crossed, too-wide, or non-tradable data
means no current actionable entry for that symbol. The report must remain Watch or No-Trade.
Unverified tradability is treated as missing data, so in practice NO row in this lane is ever
actionable.

## Candidate tradability is NOT verified in this lane

`get_equity_tradability` has `required: ["account_number", "symbols"]`. The hydrator child is
deliberately denied every account-selection path: the accounts read is outside
`broker_namespace_policy.HYDRATOR_READ_BASENAMES`, and the collector prompt forbids obtaining,
receiving, inferring, or emitting an account identifier. The bundle also asserts
`safety.contains_account_data: false`. The read is therefore structurally impossible here, and
this is by design, not a collector failure.

Consequences, all deterministic and fail-closed:

- `tradeable` and `fractional` are always `null`; the collector is forbidden to report either.
- Every row carries `tradability_not_verified_true` and `candidate_actionability_eligible: false`.
  `research_freeze.py` blocks any report line making an actionability claim about such a symbol.
- `candidate_research_price_eligible` is unaffected: a fresh, session-correct, uncrossed quote is
  still a valid candidate research price. Tradability was never required to observe a price.
- If a tradability value appears with no matching tradability read in the immutable audit receipt,
  the whole artifact is rejected with `raw_tradability_claimed_without_broker_read`. Tradability is
  never inferred from a quote response.

Anchor tradability (the SPY-style market anchor) is unaffected: it is read in the separate
`robinhood-readonly` verifier lane, which does have account context, and it is that lane which
feeds `robinhood_buy_gate`. Nothing here changes the Buy gate.

Reversing this would require an account-selection path in the hydrator lane. That is a
control-plane authority change (a broker read allowlist widening plus an extra mandatory read in a
budget that is already measured-negative) and needs explicit user authorization. It is not in
scope for this component.

## Artifacts

For target session `YYYY-MM-DD`:

| Artifact | Path | Contract |
|---|---|---|
| Policy | `config/candidate_quote_hydration.json` | Versioned bounds, allowlists, freshness, spread, and authority rules |
| Request | `data/runtime/robinhood/candidate_quote_request_YYYY-MM-DD.json` | Write-once; bound to discovery path and SHA-256 |
| Bundle | `data/runtime/robinhood/candidate_quotes_YYYY-MM-DD.json` | Write-once; self-hashed; complete requested-symbol denominator |
| Raw staging | `data/runtime/rh_candidate_quote_raw/` | Ephemeral, mode 600, trap-deleted after sealing |
| Health | `data/runtime/health/YYYY-MM-DD_candidate_quote_health.json` | Self-hashed aggregate counts and reason codes; no prices or account data |

The request includes every eligible, normalized symbol up to the fixed cap and records every
exclusion. The sealed bundle preserves one row for every requested symbol, including unavailable
rows. This prevents a collector or model from hiding failures by shrinking the denominator.

## Deterministic States

- `empty`: discovery completed but produced no eligible candidate symbols; no broker/model call.
- `ready`: every requested row is currently fresh and structurally valid.
- `partial`: at least one row is usable and at least one is unavailable, stale, or invalid.
- `unavailable`: the request is valid but no row supplies a usable current quote, including a
  missing connector, denied permission, timeout, or collector failure.
- `at_issue`: request/bundle integrity, schema, target, timestamp, denominator, or receipt binding
  is invalid, or the bundle failed its own self-check and was degraded to a named failure record.

The terminal seal ALWAYS writes exactly one bundle. A bundle that cannot self-validate degrades
to the `at_issue` shape (every row `unavailable`, zero fresh, zero actionable) with the failing
check recorded in `validation_reason_codes`, because an absent artifact is strictly less evidence
than an honest failure record.

Freshness is re-derived whenever the bundle is validated. A once-fresh immutable bundle can become
stale without being rewritten. Session labeling is also re-derived from Eastern Time market
windows; an overnight timestamp is never mislabeled as premarket.

## Collector Sandbox

`scripts/run_candidate_quote_hydrator.sh` invokes the child collector through the audited Claude
wrapper. The rendered `--allowedTools` ceiling is the two-tool hydrator read set
(`get_equity_quotes`, `get_equity_tradability`) resolved from
`broker_namespace_policy.HYDRATOR_LIVE_TOOLS`, but the prompt narrows the collector to the quote
read alone, because the tradability read cannot be satisfied without account context this lane
must never have. Narrowing the allowlist itself is a separate control-plane change.

The child has no account, portfolio, position, order, P&L, web, shell, file-read, or agent tools.
Its only file capability is writing the one raw staging path. The deterministic parent rejects
unknown fields, unrequested/duplicate symbols, raw MCP stream shapes, secret/PII-like keys or
values, mismatched request hashes, future or target-date-invalid timestamps, and source errors
masquerading as quotes. Raw staging is deleted on success and failure.

Any bundle carrying `ready` or `partial` price rows also embeds a minimized projection of the
immutable broker runtime audit receipt. Validation rechecks the receipt and event-log hash chain,
the `candidate-quotes` lane, complete instrumentation, zero preview/write/unknown-broker attempts,
and an observed quote read with no other broker read tool. Zero tradability reads is the expected
state; a tradability VALUE reported against zero tradability reads rejects the artifact. If the
receipt or its
event log disappears or changes, the bundle stops validating. This proves that the bounded tools
were attempted under the hooks. It does not cryptographically bind each model-transcribed scalar
to the MCP response, so deterministic quote semantics and every independent source/Buy gate still
apply.

Raw input is capped at 256 KiB and must be a regular non-symlink file. Main-agent Bash cannot call
hydrator mutators, import the writer, write raw/audit paths, or invoke `audited_claude.py` directly;
it can only call the sanctioned wrapper or the read-only validation commands.

Do not run the production wrapper merely to test code. It can invoke the authenticated read-only
broker connector. Use the offline suite instead:

```bash
PYTHONWARNINGS=error::ResourceWarning \
  python3 scripts/test_candidate_quote_hydrator.py
```

Read-only validation and aggregate health generation for an artifact that already exists:

```bash
python3 scripts/candidate_quote_hydrator.py validate --target YYYY-MM-DD
python3 scripts/candidate_quote_hydrator.py health --target YYYY-MM-DD \
  --out data/runtime/health/YYYY-MM-DD_candidate_quote_health.json
```

## Morning and Freeze Integration

`prompts/morning_scan.md` defines two dependency waves. The discovery wave persists the candidate
denominator, then the main orchestrator runs the hydration barrier exactly once. Candidate-specific
SEC, technical, portfolio-risk, and skeptic agents wait for its terminal bundle.

`scripts/research_freeze.py` independently validates the bundle and checks report prose. The report
must state:

```text
Candidate quote coverage: <status> — fresh <F>/<N>, actionable <A>/<N>; Robinhood read-only candidate bundle; Buy authority: none
```

The semantic fields are fixed. Any claimed candidate current price must match a currently fresh
row and disclose its timestamp and session.
Any actionable-entry claim requires that row's deterministic actionability flag. The freeze
receipt binds the request, bundle, and broker audit receipt hashes, and the outer runner rechecks
all three before accepting the terminal result.

## Failure Behavior

| Failure | Result |
|---|---|
| Discovery missing/invalid | No invented symbols; dependency unavailable |
| No eligible symbols | Valid `empty` request/bundle; no connector call |
| Connector absent or permission probe fails | Complete `unavailable` bundle |
| Collector timeout/error | Complete `unavailable` bundle; raw file removed |
| One symbol missing or bad | `partial`; a symbol the collector never returned is retained as `unavailable`, a symbol it returned without a usable market timestamp is retained as `stale` (prices present) or `invalid` (prices absent). Never `fresh`, never given an invented timestamp |
| Bundle fails its own self-check | Sealed `at_issue` bundle whose `validation_reason_codes` name the failing check; the terminal seal never leaves the session with no artifact |
| Bundle expires | Dynamic validation downgrades rows without mutation |
| Artifact tampering or discovery drift | `at_issue`; freeze prose cannot use it |
| Main model fails before normal health step | Best-effort aggregate health still emitted |

Operational health is deliberately separate from trading authority. `ready` means the quote
artifact is healthy, not that any trade is good. `unavailable` normally degrades research rather
than triggering a system-integrity alarm; structural invalidity remains `at_issue`.

## Rollback

Disable the prompt barrier and remove candidate-bundle consumption while retaining artifacts for
audit. The pre-existing fixed market-anchor snapshot and Watch/No-Trade fallback remain intact.
Rollback must not reinterpret any historical candidate bundle as market-anchor, Buy, or execution
authority.
