# Kalshi MCP research lane

Status: **dormant, shadow-reference-only, no schedule, no proposal surface, no execution**.

The `kalshi` MCP server is registered in Claude's user configuration and its read-only
implementation was health-checked on 2026-07-26. The current Codex tool registry does not expose
that server, so Codex must treat it as unavailable. This repository never assumes that a
user-scoped registration is present or healthy.

Event contracts remain prohibited by `AGENTS.md` §1 rule 5. The integration prepares an isolated
measurement layer for possible future offline calibration research; it does not authorize an
event-contract trade, an equity signal, a Buy, sizing, a ticket, or automated execution.

## Current containment

- Every existing unattended MCP lane denies the whole `mcp__kalshi` namespace in both its runner
  flags and `config/mcp_lanes/*.json`.
- `scripts/kalshi/` is a hermetic, self-manifested package. Nothing imports or schedules it.
- The connector policy positively identifies ten public market/event/series reads. Five
  private-account reads (`balance`, `positions`, `orders`, `fills`, `settlements`) are disabled
  and separately denied in project permissions.
- The future launcher copies a reviewed public-only server, generated dependency lock, pinned
  `uv`, and a complete 883-file Python base runtime into a private temporary stage. The runtime
  executable, standard library, native dependencies, and CA bundle are bound by one tree digest;
  no executable or standard-library bytes are loaded from the mutable source prefix after launch.
- The runtime MCP audit treats those public methods as research reads, treats every private or
  unknown Kalshi method as a violation, treats every unregistered MCP namespace as broker-shaped
  by default, hashes tool inputs rather than persisting them, and denies violations before
  execution. Receipts are valid only under the current settings and policy hashes.
- `config/source_authority.yaml` grants Kalshi only isolated offline calibration observation.
  It may not generate or filter equity candidates and has no hard-evidence, catalyst,
  macro-actual, equity-price, scoring, Buy, sizing, or execution authority.
- The fee model remains `unverified`; cost and edge calculations null-propagate until an operator
  verifies the current first-party fee schedule and records a new epoch.

## Connector surface

Public-only future lane allowlist:

```text
get_exchange_status
list_markets
get_market
get_orderbook
get_recent_trades
get_candlesticks
list_events
get_event
list_series
get_series
```

Disabled account reads:

```text
get_balance
get_positions
get_orders
get_fills
get_settlements
```

Any unlisted method is denied. A server-source hash or tool-surface change requires a fresh audit
and an explicit policy update; it never widens access automatically.

## Local verification

These checks do not place, preview, amend, or cancel an order:

```bash
python3 scripts/kalshi/lib/kec_connector.py check-policy
python3 scripts/kalshi/lib/kec_connector.py public-tools-json
python3 scripts/kalshi/lib/kec_connector.py audit-source \
  --source ~/kalshi-mcp/server.py
python3 scripts/kalshi/rebuild_self_manifest.py --check
```

The only repository launcher prepared for a future dedicated observation lane is:

```bash
bash scripts/kalshi/run_public_mcp.sh
```

It first verifies the exact external `server.py` bytes and tool surface, redirects `uv` writes to
a private cache, stages the complete hash-bound Python runtime, pins TLS verification to the
staged CA bundle, and unconditionally replaces inherited Kalshi authentication variables with
non-credentials. Starting it manually still grants no workflow access: a future lane must also
use strict MCP configuration and the exact public allowlist above.

Do not run the upstream `--selftest` from an unattended workflow: when account credentials are
present it also reads private account state.

## Ratification holds

- `scripts/kalshi/config/self_manifest.json` is refreshed and verifies all 24 package files.
  An operator must still review and re-sign the repository-wide `data/MANIFEST.json`; models
  are intentionally blocked from ratifying that global integrity boundary.
- The current protected Bash-path hook does not yet reject every indirect direct launch of the
  external upstream server or every indirect read of `~/.kalshi`. Exact settings denies and the
  unattended runtime audit still fail closed on the reviewed paths, but the user-scoped direct
  registration is **outside the trusted repository boundary** until the operator explicitly
  authorizes a protected-hook update. Do not allow or schedule the user-scoped registration.

## Future activation ladder

1. Keep the package and namespace isolation green under the full repository test suite.
2. Add a separate, operator-approved observation collector with strict MCP configuration and
   exactly the ten public tools above. It must persist only normalized, hash-bound data, never raw
   MCP transcripts or account fields.
3. Accumulate prospective blind forecasts and resolved outcomes in a separate shadow dataset.
   Kalshi odds may be a comparison target, never an equity candidate source, feature, or
   production score.
4. Verify fees from a current first-party schedule and open a new immutable record epoch.
5. Require out-of-sample calibration, dependence-aware effective sample size, liquidity/book-walk
   checks, and explicit evidence that any apparent edge survives fees and spread.
6. Any Watch-only event-contract publication requires an explicit constitutional amendment and
   outside-repository ratification. Automated or live trading would require a later, separate
   approval plus legal/compliance review, paper/shadow stages, an independent binary-risk gate,
   kill switches, credential separation, and write-tool controls. None exists today.

No activation step is implied by the completion of the previous one.


## Phase map — what exists, what it may claim

These are five distinct things. Conflating any two of them overstates the system.

| Phase | What it is | State (2026-08-02) | May it claim an edge? |
|---|---|---|---|
| **0** | Dormant MCP / public-data integration: hermetic `scripts/kalshi/` package, whole-namespace `mcp__kalshi` denial in every unattended equity lane, hash-pinned public-only launcher | installed, dormant | no |
| **3** | Blind commit–reveal **observation lane**: price-free forecast → hash-chained commit → deterministic public capture → mechanical resolution | installed and API-hardened; **`enabled: false`**, schedule inert | no — it measures, it does not trade |
| **4** | **Fee verification**: 90-day operator-signed receipt binding the published schedule to its snapshot bytes | tooling installed; `coefficient_status: unverified`, so every fee-dependent number null-propagates | no |
| **5** | **Skill evaluation**: pre-registered thresholds, content-addressed development/holdout split, holdout opens exactly once | tooling installed; epoch-0 pre-registered, holdout sealed | only up to `CANDIDATE_SKILL_FOR_OPERATOR_REVIEW` |
| **6** | A future **constitutional amendment** proposal for Watch-only publication | does not exist; would need typed ratification + outside-repo record | n/a |

Phase 3 is a measurement instrument, not a trading bot and not an automated
money-making system. It places no orders, proposes no positions, and its strongest
possible output is a request that a human look at a calibration report. No economic
edge has been demonstrated, and none can be until Phase 4 verifies fees and Phase 5
opens its holdout on a sample large enough to mean anything.

## Phase 3 pack (2026-08-02)

`operator_patches/2026-08-02_kalshi_phase3_observation_lane/` installs the blind
commit–reveal observation lane (11:20 PT MTWRF when activated): deterministic REST
capture of the seven-series official-agency macro universe, a strict-empty-MCP
Read-only forecast child whose argv record is demanded as commit-time blindness
evidence, hash-chained shadow datasets under `data/runtime/kalshi/shadow/<epoch>/`,
pre-registration + a one-time holdout interlock, and Phase-4 fee-receipt tooling.
Installed does not mean active: `observation_lane.json` ships `enabled: false`, and
activation is a separate operator ceremony (see the pack README, including the still
-open `--strict-mcp-config` empirical check). The capture path deliberately uses
stdlib HTTPS against the pinned public host instead of the staged MCP server — no
model and no MCP in the loop is a strictly smaller surface, and "never raw MCP
transcripts" is satisfied by never having one. `execution_enabled` remains false;
event contracts remain prohibited instruments; the three kec_policy inertness locks
remain engaged.
