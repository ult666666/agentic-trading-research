# Caller-dated strategy shadow forward engine

`scripts/quant/strategy_shadow_forward.py` is the deterministic state engine between a frozen
shadow plan and a future observation monitor. It generates hypothetical, normalized-capital
results one valid US market session at a time from exact immutable local session packages and
daily-bar inputs.

The engine is caller-dated, not independently prospective. Caller-supplied timestamps are
canonical metadata declarations; they are not a trusted-clock receipt and cannot prove that a
specification or result was created before the session or was not backfilled. Establishing that
property requires a future trusted outer clock receipt plus a canonical plan/strategy/session
singleton registry.

The engine is a library contract, not a scheduler. It is not wired into the morning, post-open, or
post-close workflows and cannot start a shadow plan. A real run still requires an eligible
evidence dossier, a complete incumbent cohort, a replay specification frozen before its first
session, adequate point-in-time data, and explicit human approval.

## Artifacts

- `quant_strategy_shadow_replay_spec/1.0` freezes the plan/strategy identity, variant parameters,
  simulation assumptions, allocation, decision rules, historical anchors, supported instruments,
  exact policy/calendar hashes, and a shared expected-run denominator self-commitment. The same
  commitment names exactly one candidate, one benchmark, and a sorted unique nonempty incumbent
  roster in every spec.
- `quant_strategy_shadow_forward_result/1.0` advances exactly one strategy and one market session.
  It binds every input, opening state, decisions, costs, position events, terminal state, and the
  exact previous result/state hashes.
- `quant_strategy_shadow_forward_run/1.0` requires exactly one candidate, one continuous SPY
  benchmark, and every frozen incumbent exactly once. All strategies share the start session,
  simulation assumptions, policy contracts, target session, cutoff, and exact market-bars
  artifact. Candidate and incumbent results also share the exact session feature package.

The expected-run denominator is intentionally fail-closed but not authoritative: it is a
caller-frozen self-commitment embedded in and hashed by every replay spec. It prevents a later run
caller from deleting an incumbent from both the spec list and result list while retaining the
already-frozen surviving specs. It does **not** validate the bytes behind `plan_sha256`, prove
human approval, supply a trusted freeze time, or prevent a same-user writer from rebuilding and
re-sealing the entire spec set with a smaller roster. Those stronger claims require the future
approval-bound outer plan registry; this standalone engine makes none of them.

The public construction and validation seams are:

- `build_replay_spec` / `validate_replay_spec`;
- `build_forward_result` / `validate_forward_result`;
- `build_run_receipt` / `validate_run_receipt`; and
- `write_json_once` for path-local create-once, idempotent-same-bytes outputs. It is not a
  canonical plan/strategy/session singleton and supplies no publication authority.

## Simulation rules

The engine is long-only and accepts only US-listed common stock and unleveraged ETF research
identities. It does not accept caller-supplied return scalars.

For candidate and incumbent strategies it:

1. accepts complete signals only from the exact immutable session package; a replaceable feature
   corpus is discovery-only and always produces `unavailable`;
2. revalidates every package member from the same securely read bytes, rederives feature rows from
   the package event and prior-bar members, and binds prior-close lineage to the exact settlement
   bars through the immediately previous valid trading session;
3. requires event-observation and feature-knowledge cutoffs strictly before the target-session
   open, eligible rows, supported instruments, positive prior close/ATR, and target-session bars;
4. models a next-open entry only when adverse entry slippage remains below the frozen maximum;
5. treats `transaction_cost_bps_per_side` as an explicitly **post-stress** frozen floor and
   rejects a spec when that floor is below the cost-policy minimum multiplied by the selected
   stress multiplier. Candidate/incumbent estimates apply that multiplier internally exactly
   once and use the stricter of the estimate and frozen floor; the benchmark uses the same
   validated post-stress floor without multiplying it again;
6. enforces frozen portfolio position, gross/net, sector, catalyst, aggregate-stop-risk, cash, and
   entry-count limits. Planned stop risk includes the known frozen stop slippage and both modeled
   cost sides; gaps, halts, and worse realized slippage can still exceed it;
7. charges entry and exit costs once;
8. models gap-through-stop at the opening price with stop slippage;
9. uses stop-first when a daily bar touches both stop and target;
10. exits at the frozen target or the maximum holding-session close; and
11. carries cash and surviving positions into the next session.

The SPY benchmark enters once on its first complete session, pays the modeled entry cost once,
and remains continuously marked. It is not liquidated and restarted each day.

## Missing-data semantics

An exact validated session package with zero events is a valid zero-signal session. A feature
corpus—even if internally self-hashed—cannot establish that outcome. Missing, malformed,
conflicting, late, replaceable, or incomplete inputs produce `status: unavailable` instead:

- return availability is false;
- top-level and terminal session returns are `null`, never numeric zero;
- no mark, entry, exit, or cost is fabricated;
- the last verified state is carried with an explicit unavailable-state marker; and
- every later session remains unavailable with `prior_state_unresolved`, because resuming from a
  day on which a stop, target, or time exit may have occurred would create survivorship bias.

A package containing an unsupported instrument type makes the session unavailable rather than
emitting a Python-valid/Schema-invalid decision. OHLC prices outside the engine's explicit
$0.0001-to-$1,000,000,000 per-share arithmetic bounds are also unavailable before any slippage
or level calculation, preventing finite-input overflow from becoming a nonfinite research result.

Recovery requires a separately frozen rebuild of the complete chain, not mutation of an existing
create-once result.

## State and tamper validation

After the genesis result, every session must reference the immediately preceding valid market
session. Validation recursively replays the full previous-result chain to genesis, with a 504-link
bound and path-cycle detection. Recomputing only the outer hash of a forged prior NAV or position
state is insufficient: exact input and semantic rederivation must still match.

All state, position, decision, event, denominator, manifest, NAV-row, result, and run objects carry
canonical hashes. Policy, cost, portfolio, and market-calendar contracts are revalidated from
their exact local bytes. Authority-bearing reads use bounded `openat`/no-follow snapshots with
pre/open/post metadata agreement, reject symlinks and hardlinks, and derive JSON and file hashes
from the same bytes. Strict JSON rejects duplicate keys, nonfinite constants, and overflowing
numbers. Outputs remain path-local research artifacts, not canonical publications.

## Authority and interpretation

Every price, position, fill, cost, and return is hypothetical research. Daily OHLC data cannot
establish intraday sequence; conservative stop-first handling reduces but does not remove that
limitation. Modeled costs are not verified fills, normalized capital is not account equity, and a
positive shadow result cannot establish future profitability.

The engine has no network, model, broker, account, order, process, production, Buy, freeze, risk,
metric, strategy-promotion, or execution authority. Integration into the existing shadow monitor
is a separate change and must preserve the human-approval and fixed-checkpoint gates.
