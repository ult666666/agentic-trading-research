# Options Research Lane (v0) — Operator & Developer Guide

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

**Shadow-only. Research-only. Options execution is permanently disabled.** Ratified
2026-08-02 (`journal/2026-08-02_options_kickstarter_ratification.md`); mandate text in
CLAUDE.md/AGENTS.md §3c; governance in `config/governance.yaml
options_research_overlay`; delivered by
`operator_patches/2026-08-02_options_research_kickstarter_v0/`.

## What this lane is

An **expression overlay**, not an idea generator: only an underlying that fully survived
the equity decision path with an explicit bullish/bearish thesis may be evaluated for a
defined-risk long option expression (long call for bullish, long put for bearish — never
converted from a Hold/Reduce/Exit). Output is one of exactly three statuses:

| Status | Meaning |
|---|---|
| `BLOCKED` | any hard screen, account gate, provenance gate, or sizing floor failed (fail-closed default) |
| `PREOPEN_WATCH_ONLY` | structurally sound but built on premarket/previous-close marks — never actionable |
| `SHADOW_OPTIONS_CANDIDATE` | fresh regular-hours, fully gated shadow evaluation — still NOT actionable (shadow phase) |

There is no BUY, no OFFICIAL, no ticket. Option price targets and option risk/reward are
emitted as `UNAVAILABLE_MODEL_UNVERIFIED` until an independently reviewed repricing model
exists — fabricating them is structurally impossible in v0, and that condition also
blocks graduation.

## Components

| Piece | Path |
|---|---|
| Protected limits (D2 defaults) | `config/options_risk_limits.yaml` (hook-protected; change-control only) |
| Chain validator | `scripts/validate_options_chain_snapshot.py` (positive allowlist, hash-bound raw→normalized, per-contract reason codes) |
| Options risk gate | `scripts/lib/options_risk_gate.py` (parallel to the equity gate — the equity gate still rejects options) |
| Deterministic selector | `scripts/options_contract_selector.py` (fixed preference order + total-order tie-breakers) |
| Shadow runner | `scripts/options_shadow_runner.py` (fixture/dry-run/explicit-input modes; own lock; atomic artifacts; zero broker calls) |
| Options ledger | `scripts/options_research_metrics.py` → `data/options_research_ledger.json` (append-only, hook-protected, isolated from equity ledgers) |
| Capability receipt | `scripts/lib/options_policy.py` → schema `schemas/options_capability_receipt.schema.json` (UNKNOWN = BLOCKED) |
| Snapshot schema | `schemas/options_chain_snapshot.schema.json` |
| Disabled schedule spec | `config/options_shadow_schedule.json` (09:45 ET design point; NOT installed) |
| Tests | `scripts/test_options_*.py` (8 suites) |

## Running it (fixture mode — works on non-trading days)

```bash
cd ~/trading-system
python3 - <<'EOF'
import json, sys
sys.path[:0] = ["scripts", "scripts/lib"]
import options_fixtures as fx
json.dump(fx.raw_chain(), open("data/runtime/options_research/demo_raw.json", "w"))
json.dump(fx.context(), open("data/runtime/options_research/demo_ctx.json", "w"))
EOF
python3 scripts/options_shadow_runner.py \
  --raw-snapshot data/runtime/options_research/demo_raw.json \
  --context data/runtime/options_research/demo_ctx.json --dry-run
```

Live mode consumes a collector-produced raw snapshot plus `--require-capability
--capability-receipt <path>`; the receipt must be READY (every capability positively
verified, including the attended regular-hours read-only probe) or the run is BLOCKED.

## Hard boundaries (do not relax without a new ratification)

- Option order/exercise tools: triple-denied (settings deny-list both namespaces,
  fail-closed `protect_broker_tools.py`, `validate_broker_permissions.py` cross-check).
- The options modules import no network stack, no subprocess machinery, no broker client
  (regression-tested by static scan).
- `data/options_research_ledger.json` + `config/options_risk_limits.yaml` are protected
  from direct model writes by both hook families (both `.claude/` and `.codex/` copies).
- Premarket marks: `PREOPEN_WATCH_ONLY` ceiling, always.
- Binary event inside the holding window: BLOCKED (`allow_pre_event_long_premium: false`).
- Buying power is never treated as settled cash.
- Graduation: ≥10 complete shadow sessions AND ≥30 eligible observations, then a SECOND
  explicit user ratification. Ledger counters are report-only and cannot graduate anything.

## v1 additions (2026-08-03) — deterministic analytics, scorer, manual ticket

v0 proved a contract could be *selected and gated*. v1 adds the model-free analysis that
was missing, and enforces it as a further fail-closed gate.

| Piece | Path |
|---|---|
| Payoff/exposure analytics | `scripts/lib/options_analytics.py` (expiration-intrinsic only; refuses every non-ratified structure) |
| Research scorer | `scripts/options_research_scorer.py` (0–100 → RESEARCH_CANDIDATE / WATCH / AVOID / NO_TRADE) |
| Proposed manual ticket | `scripts/options_manual_ticket.py` → `ticket.json` + `ticket.md` |
| Ticket schema | `schemas/options_research_ticket.schema.json` |
| Pending-authorization gate | `data/options_pending_authorizations.json` (every entry `authorized: false`) |
| Debit-spread ratification packet | `journal/2026-08-03_options_debit_spread_ratification_packet.md` (PROPOSAL, not ratified) |

**What analytics computes** (all deterministic, no option pricing model): maximum loss,
bounded maximum profit, breakeven, the favorable underlying move required, scenario P&L at
target 1 / target 2 / invalidation, risk-reward at expiration, dollarized theta decay over
the intended hold, dollarized vega exposure per IV point, catalyst-vs-expiration timing fit,
a delta-based ITM **proxy** labeled `ESTIMATE_NOT_A_FACT`, assignment/exercise notes, and a
data-completeness record.

**What it still refuses to invent:** `option_target_1`, `option_target_2` and the
option-premium risk/reward remain `UNAVAILABLE_MODEL_UNVERIFIED`. Expiration-intrinsic
figures are a different quantity and are labeled as such everywhere.

**New fail-closed gates** (all TIGHTENING — none can upgrade a result): missing IV; missing
delta/theta/vega (gamma missing is a warning); unverified Greek units; catalyst dated after
expiration; catalyst dated exactly on expiration; expiration before the planned exit;
target 1 not clearing breakeven; zero contracts; non-positive premium; non-standard
multiplier; a fee estimate offered without an official fee model; any non-ratified
strategy; any multi-leg payload; any short leg.

A blocked analytics result downgrades a would-be `SHADOW_OPTIONS_CANDIDATE` to `BLOCKED`
with `blocked_analytics:<code>` and zero size. `PREOPEN_WATCH_ONLY` keeps its ceiling and is
classified `WATCH` (premarket marks are not sizeable, so they are not scoreable).

**Debit spreads are NOT ratified.** The payoff mathematics for call and put debit spreads is
implemented and tested (so a future ratification does not rest on unproven arithmetic), but
`analyze()` refuses them, the selector never assembles two legs, and
`config/options_risk_limits.yaml` still carries `allow_multi_leg: false`. The remaining
authorization gate is machine-readable in `data/options_pending_authorizations.json` and
written up in the ratification packet.

### Artifacts per shadow run (v1)

`data/runtime/options_research/<market_date>/<run_id>/` now contains `snapshot_sanitized.json`,
`analytics.json`, `research_score.json`, `ticket.json`, `ticket.md`, `decision.json` and
`report.md`. The ticket hard-codes `execution_enabled: false`,
`broker_write_authority: false`, `manual_ticket_only: true` and `broker_calls_made: 0`;
those four values are constants in the renderer and are additionally constrained by the
ticket schema, so a mutated ticket fails validation.

## Review corrections (2026-08-04) — fee truthfulness and ticket consistency

An independent review of the v1 commit returned **CHANGES_REQUIRED** on two defects. Both
were reproduced against the v1 code before being corrected; neither widened any authority,
and the corrections only tighten.

**1. The fee disclosure was false.** Naming an official fee reference flipped
`fees.all_figures_are_gross_of_fees` and the scenario-level `gross_of_fees` labels to
`false`, and removed the scorer's costs-not-modeled penalty — while **not one figure had a
cent subtracted**. Premium at risk, maximum loss, maximum profit, scenario P&L, return on
premium, breakeven and risk/reward were byte-identical with and without the reference.

The corrected contract is **gross-only**:

| Field | Meaning |
|---|---|
| `fees.fee_reference_present` | an official fee schedule was named |
| `fees.fee_reference_source` | which one |
| `fees.fee_semantics_complete` | whether it states opening, closing AND round-trip cost separately |
| `fees.costs_modeled` | **hard-coded `false`** — no cost is subtracted anywhere |
| `fees.all_figures_are_gross_of_fees` | **hard-coded `true`** |
| `fees.estimated_net_figures` | `UNAVAILABLE_INCOMPLETE_COST_MODEL` |

`official_model_present` is retained for API compatibility and has only ever meant that a
**reference** exists. Consumers deciding whether costs were actually deducted must read
`costs_modeled`. The scorer's `fee_model_unavailable_costs_not_modeled` penalty is now keyed
on `costs_modeled`, so naming a fee schedule can never buy it back. The manual ticket states
in plain language that no fee deduction was applied and that realized results will be lower.
A complete, independently validated net-cost model is a **separately ratified future
change**; until it exists no net figure is produced and none is labeled net.

Fee-model input now fails closed on an unknown field, a negative fee, NaN/infinity, and a
bool masquerading as a number, in addition to the existing "estimate without an official
source" block.

**2. A ticket could contradict itself.** The renderer assigned `classification` to itself,
so a `SHADOW_OPTIONS_CANDIDATE` gate with `OK` analytics and **no surviving contract**
produced a `WATCH` ticket whose own body said "no contract survived the deterministic
gates", carried `legs: null` and size 0, and recommended "NO TRADE".

One deterministic matrix (`_resolve_disposition`) now resolves classification, reason codes,
legs, proposed size and the final recommended action together:

| Gate | Analytics | Contract | Result |
|---|---|---|---|
| `BLOCKED` | any | any | `NO_TRADE`, size 0, legs null |
| any | `BLOCKED` | any | `NO_TRADE`, size 0, legs null |
| `PREOPEN_WATCH_ONLY` | not blocked | any | `WATCH` at most, size 0, watch-only final action |
| `SHADOW_OPTIONS_CANDIDATE` | `OK` | valid, size ≥ 1 | scored classification retained |
| `SHADOW_OPTIONS_CANDIDATE` | `OK` | `None` / `{}` / malformed | `NO_TRADE` + `no_contract_survived_deterministic_gates` |
| unrecognized | any | any | `NO_TRADE` + `gate_status_unrecognized` |

The stable reason codes are published in the new required ticket field
`classification_reason_codes`, so the schema version is `options_research_ticket/1.1`.
No ticket can carry `WATCH`/`RESEARCH_CANDIDATE` and simultaneously say NO TRADE because
nothing survived, and no branch introduces BUY, SELL, actionable, executable, broker-ready
or order language.

**Coverage gaps closed alongside them:** live-path reason-code tests for
`blocked_non_positive_price_input` and `blocked_invalid_date_input`; a defense-in-depth
`intended_holding_days` boundary at the analytics edge (`1..10`, mirroring the PROTECTED
`config/options_risk_limits.yaml` and pinned against it by test, emitting
`blocked_intended_holding_days_outside_policy`); explicit
`CONSTANT_THETA_LINEAR_APPROXIMATION` disclosure on the modeled decay (machine-readable and
in the ticket markdown — the arithmetic is unchanged, only now labeled as an approximation
that can materially understate decay near expiry); and a loud, never-silent `jsonschema`
requirement so the ticket-schema authority-expansion mutation tests can no longer disappear.

`option_target_1`, `option_target_2` and the option-premium risk/reward remain
`UNAVAILABLE_MODEL_UNVERIFIED`. Debit spreads remain **PROPOSED_NOT_RATIFIED /
NEEDS_RATIFICATION**. `execution_enabled` and `options_execution_enabled` remain permanently
false.

## Rollback

`options_research_enabled: false` in `config/governance.yaml` (keep the overlay record;
ledger history is never rewritten). File-level rollback: pack `backup/` via a reviewed
operator patch.
