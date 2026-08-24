# Manual Execution Observation Boundary

Status: implemented 2026-07-11; automatic registry sync + post-close health added 2026-07-11
(WP-14, §7a). Observation-only and diagnostic-only. No dedicated scheduler job exists; registry
sync piggybacks on the freeze path and the existing post-close job, while event recording stays
manual. Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## 1. Purpose

The research system can freeze a proposed manual ticket, but it does not place orders. This layer
records what a human later says happened, or what a separately created sanitized read-only broker
receipt says was observed. It closes the evidence gap between:

1. the frozen proposal;
2. the human decision;
3. a human-entered order observation;
4. partial or complete entry-fill observations; and
5. the observed terminal state.

It does not change the proposal, authorize an action, call a broker, confirm an order, calculate
account performance, or feed a production gate.

## 2. Contracts

The versioned policy is
[`config/manual_execution_observation_policy.json`](../config/manual_execution_observation_policy.json).
The executable boundary is
[`scripts/manual_execution_observation.py`](../scripts/manual_execution_observation.py).

```text
freeze_candidates/1.0
        |
        v
manual_execution_case_registry/1.0       complete candidate denominator
        |
        v
manual_execution_observation_event/1.0   append-only hash chain
        |
        v
manual_execution_observation_summary/1.0 immutable derived snapshot
```

The schemas are:

- `manual_execution_observation_policy/1.0`;
- `manual_execution_case_registry/1.0`;
- `manual_execution_observation_event/1.0`;
- `manual_execution_source_receipt/1.0`; and
- `manual_execution_observation_summary/1.0`.

Defaults belong under `data/runtime/manual_execution_observations/`, which is local runtime data,
not a tracked account ledger.

## 3. Complete denominator

`initialize` reads a validated frozen `candidates.json` and creates exactly one deterministic case
for every candidate. Buy, Watch, Reject, Avoid, pending-decision, no-action, and no-fill cases stay
in the registry. A successful fill cannot be selected into the dataset after its outcome is known.

Only a candidate that was already all of the following is marked
`proposal_eligible_for_manual_action=true`:

- Official Research;
- classified `Buy`;
- deterministically `buy_authorized=true`; and
- `counts_in_metrics=true` in the frozen candidate artifact.

The observation layer cannot grant any of those fields. If a human reports entering a Watch or
other ineligible case, the fact can be retained for audit, but the summary sets
`policy_breach_observed=true`. It never rewrites the original classification.

## 4. Evidence levels

Two evidence levels are allowed:

| Source | Stored label | Meaning |
|---|---|---|
| Human attestation | `human_attested_unverified` | The human says the event occurred. It is not broker confirmation. |
| Sanitized read-only receipt | `receipt_anchored_unconfirmed` | The local receipt and source hash match. This observer still did not call or independently verify the broker in the current session. |

A broker receipt must name one of four exact order/trade history tools already present in the
project's deterministic read-only allowlist. Decision events cannot use a broker receipt because a
broker order-history response cannot establish the human's earlier decision. Preview, place,
cancel, watchlist-write, authentication, and unknown tools are rejected.

The receipt schema excludes account identifiers, broker order IDs, credentials, cookies, names,
addresses, and free-form payloads. It contains only the local case ID, event time, sanitized facts,
an allowlisted tool name, a hashed session fingerprint, and negative safety assertions.

## 5. Append-only state machine

The effective event path is:

```text
decision(accept)
    -> order_entry(buy only)
        -> zero or more unique fills
            -> terminal(filled | partial_fill | no_fill | canceled | expired | unknown)
```

Reject, no-action, or unknown decisions cannot acquire an order event. A fill cannot precede an
order; a terminal cannot precede an order; cumulative fill quantity cannot exceed intended
quantity. `filled`, `partial_fill`, and `no_fill` terminal states must agree with quantities.

Corrections never edit old bytes. A new record may supersede one earlier record of the same case
and event type. Fill corrections must preserve the local fill ID. The entire effective state is
revalidated before the correction is appended. Sequence, parent hash, draft hash, record hash,
source receipt hash, and source semantics are all checked.

## 6. Diagnostics

For observed buy-side fills, the summary calculates:

```text
fill_vwap = sum(fill_quantity * fill_price) / sum(fill_quantity)
fill_ratio = filled_quantity / intended_quantity
entry_shortfall_bps = 10,000 * (fill_vwap / proposal_reference_price - 1)
```

Positive entry shortfall is worse for a buy. The same diagnostic is calculated against the frozen
maximum entry when available. Time-to-first-fill and time-to-last-fill use the observed order-entry
time. Missing inputs remain `null`.

These are local implementation diagnostics, not Rule 605 statistics. SEC guidance makes order
receipt time and the contemporaneous NBBO essential to effective-spread and price-improvement
benchmarks, and treats partial executions using executed-share weighting. This layer uses
share-weighted VWAP, but it has neither a market-center order-receipt timestamp nor a timestamped
NBBO, so `effective_spread_bps` is always `null` and no best-execution claim is allowed:

- [SEC Rule 605 FAQ, April 1, 2026](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/frequently-asked-questions-rule-605-regulation-nms)
- [SEC trade-execution investor guidance](https://www.sec.gov/about/reports-publications/investorpubstradexec)

The proposal-versus-fill comparison follows the implementation-shortfall idea of comparing paper
intent with observed implementation, but this first version covers entry only. It does not include
an exit, opportunity cost for an unfilled order, commissions, taxes, or realized account return:
[Perold, "The Implementation Shortfall: Paper versus Reality"](https://doi.org/10.1515/9781400829408-014).

## 7. CLI

Validate the disabled-by-construction boundary:

```bash
python3 scripts/manual_execution_observation.py check
```

Create the complete case registry after a candidate artifact exists:

```bash
python3 scripts/manual_execution_observation.py initialize \
  --candidates data/runtime/research/YYYY-MM-DD/candidates.json \
  --out data/runtime/manual_execution_observations/YYYY-MM-DD/registry.json
```

Prepare one strict draft from a JSON input and append it:

```bash
python3 scripts/manual_execution_observation.py prepare \
  --input /path/to/event-input.json \
  --out data/runtime/manual_execution_observations/YYYY-MM-DD/drafts/event.json

python3 scripts/manual_execution_observation.py append \
  --registry data/runtime/manual_execution_observations/YYYY-MM-DD/registry.json \
  --ledger data/runtime/manual_execution_observations/YYYY-MM-DD/events.jsonl \
  --draft data/runtime/manual_execution_observations/YYYY-MM-DD/drafts/event.json
```

Write and validate an immutable summary snapshot:

```bash
python3 scripts/manual_execution_observation.py summarize \
  --registry data/runtime/manual_execution_observations/YYYY-MM-DD/registry.json \
  --ledger data/runtime/manual_execution_observations/YYYY-MM-DD/events.jsonl \
  --out data/runtime/manual_execution_observations/YYYY-MM-DD/summary-<UTC>.json

python3 scripts/manual_execution_observation.py validate-summary --input <summary-path>
```

Registry, prepared draft, and summary outputs are write-once. The event ledger is append-only and
uses an exclusive file lock plus `fsync`.

## 7a. Automatic registry sync and health (WP-14)

[`scripts/manual_execution_observation_sync.py`](../scripts/manual_execution_observation_sync.py)
initializes and monitors the registry deterministically once a frozen candidate artifact exists.
It reuses the APIs above and duplicates none of the registry, hashing, state-machine, or privacy
logic. A candidate must validate structurally and rederive against the target's freeze receipt
before it can initialize a registry. The receipt itself is independently validated for canonical
report path, exact report-byte hash, target/classification consistency, official/Buy flags, and
`execution_enabled=false`; matching two mutually tampered files is insufficient. It is invoked in
three places, all strictly best-effort:

1. `scripts/research_freeze.py` runs `sync` immediately after an Official freeze has produced and
   validated `candidates.json`. Every terminal receipt is create-once; a Preliminary can never be
   promoted in place. A Preliminary freeze defers registry initialization until the runner has
   exactly reconciled that terminal, preventing the registry from binding before final wrapper
   validation.
2. `scripts/run_morning_scan.sh` performs the terminal sync after final candidate health. This
   initializes Preliminary-only sessions and idempotently confirms Official sessions. The freeze
   is already complete; sync failure can never rewrite, block, or reclassify it.
3. `scripts/run_post_close_review.sh` runs `sync` as deterministic catch-up, then writes the
   read-only health artifact
   `data/runtime/health/<target>_manual_observation_health.json`, which the post-close narrative
   renders (see `prompts/post_close_review.md`). No new launchd job exists; the existing
   post-close job is reused.

```bash
python3 scripts/manual_execution_observation_sync.py sync     --target YYYY-MM-DD
python3 scripts/manual_execution_observation_sync.py health   --target YYYY-MM-DD [--out PATH]
python3 scripts/manual_execution_observation_sync.py validate --input PATH
```

Deterministic states, fail-closed and never repaired in place:

| State | Meaning |
|---|---|
| `waiting_for_candidates` | No frozen `candidates.json` for the target. Nothing is created; no fake registry ever exists. |
| `registry_created` | Sync built the complete write-once registry, including a valid zero-candidate registry. |
| `registry_current` | The existing registry re-validates against the candidate artifact; it is never rewritten. |
| `registry_not_initialized` | Health-only: a valid `candidates.json` exists but sync has not run. Health never creates anything. |
| `at_issue` | Hash/source/semantic drift, a tampered or resealed registry, an invalid event ledger, an orphaned ledger/summary, or an invalid summary — reported with explicit reason codes, never overwritten, repaired, or resealed. |

A missing event ledger after initialization is `observation_state=no_observations` and pending
human decisions are the expected `collecting` state — neither is an incident. Watch, Reject,
no-action, and no-fill cases always stay in the denominator. A summary superseded by newer
append-only events is verified by full rederivation at its recorded ledger prefix and labeled
`superseded_by_newer_events`, never mistaken for tampering. Every sync/health output carries the
complete authority invariants (`research_only=true`, `diagnostic_only=true`,
`execution_enabled=false`, `broker_access=none`, `broker_tool_invoked=false`,
`order_preview_allowed=false`, `order_mutation_allowed=false`,
`system_claims_broker_confirmation=false`, `feeds_production=false`, `feeds_buy_gate=false`,
`counts_in_performance=false`, `realized_return_allowed=false`,
`strategy_change_allowed=false`) and is self-hashed; `validate` checks structure and seal. The
sync layer never fabricates a decision, order, fill, exit, or an empty "successful" observation,
and never calls any broker tool. The adversarial suite is
[`scripts/test_manual_execution_observation_sync.py`](../scripts/test_manual_execution_observation_sync.py).

## 8. Current limit and next gate

The boundary is implemented and registry initialization is now automatic (Official freeze,
terminal Morning sync, plus post-close catch-up, §7a), but there is no populated reviewable
human-fill corpus yet. Event
recording remains manual by design: human decisions do not exist at a deterministic scheduler
deadline, and the project has no standing permission to export account data or contact a broker.
Automatic consumption of separately produced sanitized read-only receipts still requires real
fixtures and another narrow review; no new launchd job was added.

Until then:

- `execution_enabled=false`;
- no broker tool is invoked;
- no order is previewed, placed, modified, or canceled;
- no observation counts as executed-trade performance;
- `effective_spread_bps=null`;
- `realized_return=null`; and
- no strategy, risk, Buy, freeze, source, or promotion gate changes.

The adversarial suite is
[`scripts/test_manual_execution_observation.py`](../scripts/test_manual_execution_observation.py).
