# Post-Morning Engineering Audit

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Boundary

The post-Morning engineering audit is a deterministic local observer. It is not a research stage
and has no report, source, market-data, candidate, Buy, broker, account, order, model, network,
MCP, email, or execution authority. The market report remains the only market-decision layer.
An audit can be Green while a report is No Trade, or Red while an independently valid market
report remains visible; neither layer rewrites the other.

The finality guard has six outcomes: `OFFICIAL`, `NO_TRADE`, `PRELIMINARY`, `FAILURE`,
`NOT_STARTED`, and `DEFERRED`. It only reads the receipt-checked market-report selection,
`freeze_completion_probe`, the research ledger, and the supervised Morning attempt. It cannot
promote Preliminary, synthesize a candidate, backfill research, or create ledger finality.

## Supervisor, trigger, and bounded repair

`scripts/morning_audit_supervisor.py` is the sole production caller around the existing audit. The
ordinary hook runs in `scripts/autonomy_orchestrator.py` only after the natural Morning child has
terminated and the attempt is durable. The existing watchdog invokes the supervisor after 06:16
PT as a no-start/missed-hook fallback. No LaunchAgent was added. An open audited writer window or
nonterminal attempt returns `DEFERRED`; the next existing watchdog checkpoint retries within the
policy's bounded 3,000-second interval. The production supervisor accepts only the current PT
trading date; a prior or future target is rejected before any observer write, so this surface
cannot become a historical backfill mechanism.

Every call first runs a local, read-only `preflight_self_check`. It proves that the exact policy
safety switches remain false, the remediation allowlist is exactly `rebuild_history_index`, the
canonical `tzutil.stamps(...)["utc"]` trailing-` UTC` value is readable by the audit parser, and
all `_utc` fields in the existing research ledger are readable, timezone-aware, and not future
dated. The check stable-reads and hashes its inputs; it writes nothing.

The supervisor calls `audit_run(update_backlog=False)`, so an unattended production audit never
touches tracked `journal/improvement_backlog.json`. It attempts an audit once. Only an audit error
specifically identifying the derived history index may invoke `rebuild_history_index`, followed by
one final audit retry. There is no loop beyond those two audit calls, and no automatic model,
provider, network, broker, report, research-ledger, source, configuration, schedule, service, Git,
or tracked-backlog repair.

Failures are reduced to bounded, secret-safe tokens and classified as `RETRYABLE`,
`NONRETRYABLE_CODE`, or `NONRETRYABLE_EVIDENCE`. A nonretryable failure is suppressed only while
the target's natural-run ID and the complete implementation fingerprint are unchanged. The
fingerprint binds the supervisor, audit, finality guard, timezone utility, improvement projector,
and policy hashes. A code or policy hash change therefore admits one new attempt automatically.

## Runtime observer state

The supervisor atomically maintains one strict, self-hashed runtime-only state:

```text
data/runtime/morning_engineering_audit/observer/<trading-date>.json
```

Its state is exactly `COMPLETED`, `DEFERRED`, `FAILED`, or `SKIPPED`. It binds the run and
implementation fingerprints, preflight proofs, audit call count, bounded repair result, sanitized
reason, retry class, audit projection, iteration projection receipt, warnings, and explicit
no-authority assertions. Duplicate JSON keys, symlinks, hard links, malformed/future timestamps,
unknown fields, hash mismatches, and self-hash tampering fail closed. A malformed prior observer is
preserved rather than overwritten.

Charles Pulse reads this observer as a separate engineering overlay. A terminal Morning run that
is due but lacks a valid observer is shown as engineering `FAILED`; a valid immutable audit may
still show `COMPLETED` if an older observer is absent. This overlay never changes, suppresses, or
promotes the independently validated market-report classification, Official status, Buy authority,
or execution state.

Both paths derive the same audit idempotency key from schema, target trading date, and natural run ID.
One target lock serializes publishers. A completed run directory is create-once. A second call
with identical source hashes is an idempotent skip; changed source bytes for the same run are a
hard conflict and never overwrite the first audit. Dry-run/synthetic attempts do not produce an
audit or enter trend denominators; the guard's explicit `morning-not-started-<date>` identity is
the one bounded exception used to record a due natural Morning that never began.

## Run-bound stage evidence

`scripts/morning_stage_telemetry.py` writes the exact natural run ID, UTC/PT timestamps, elapsed
time, remaining total-stage budget, stage/status, and a secret-free detail token. The Morning
runner dual-writes its existing compatible timeline and the new run-bound receipt. The production
prompt records both bounded research waves, candidate-quote wait, adjudication, final synthesis,
and freeze-request entry. The outer runner records request validation and freeze entry/exit.
Missing fields remain `UNKNOWN`.

This telemetry is observability only. It is never accepted by evidence, scoring, risk, freeze,
Buy, or execution gates.

## Immutable artifacts

Each natural run produces one directory under:

```text
data/runtime/morning_engineering_audit/<trading-date>/<idempotency-key>/
  audit.json
  summary_zh.md
  receipt.json
```

`audit.json` contains the source inventory and hashes, run/finality timestamps, stage timeline,
budget and critical path, provider health, mechanically proven facts, strongly supported
inferences, unknown evidence gaps, issue fingerprint, deterministic score, one next improvement,
and last-5/last-20 natural-run trends. `summary_zh.md` is the deterministic Chinese projection.
The receipt binds both files, exact source hashes, audit start/end, the explicit no-inline-backlog
result, protected before/after observations, post-commit history-index remediation intent, and the
no-research/no-model/no-network/no-broker/no-trade assertions. The separate iteration receipt below
is the only artifact that may attest a lifecycle projection.

Symlinks, hard links, out-of-root files, malformed JSON, future timestamps, oversized evidence,
unstable reads, and expected-hash mismatches are rejected. Missing evidence is either absent from
the inventory and reported `UNKNOWN`, or makes the selected evidence set fail closed; it is never
invented.

`data/runtime/morning_engineering_audit/history_index.json` is derived only from validated,
immutable audit+receipt pairs. It is rebuildable and is never a second source of truth.

## Reliability score

The score is deterministic and totals 100:

| Component | Maximum |
|---|---:|
| Terminal finality | 30 |
| Run-bound stage evidence | 20 |
| Timing and budget evidence | 15 |
| Provider/preflight health | 15 |
| Evidence integrity | 10 |
| Safety invariants | 10 |

Green is 85–100, Amber is 60–84, and Red is below 60. The score measures engineering
reliability only; it never measures trade quality or changes actionability.

## Runtime iteration backlog and receipt

Only a newly created, receipt-validated audit may project its issue lifecycle into the explicitly
non-authoritative runtime view:

```text
data/runtime/morning_engineering_audit/iteration_backlog.json
data/runtime/morning_engineering_audit/iteration/<date>/<audit-idempotency-key>/
  intent.json
  projection.json
  receipt.json
```

The create-once receipt binds the audit hash, audit-file and audit-receipt hashes, natural run,
audit idempotency key, projection path, exact post-write projection hash, and no-research/no-model/
no-network/no-broker/no-report/no-ledger/no-config/no-Git assertions. The projection is a process-
improvement view only and is never market evidence or authority. An idempotent watchdog repeat
validates the receipt and cannot increment the lifecycle again. If the process stops between the
projection write and receipt seal, the create-once intent and exact projection snapshot allow the
same audit to seal or finish without applying the lifecycle transition twice. A projection failure
leaves the valid audit and market report unchanged, records a retryable observer deferral, and has
no success receipt.

A fingerprint has one row with first/last seen, occurrence count, source audit hashes, and the
lifecycle `OPEN`, `PROPOSED`, `IMPLEMENTED`, `MONITORING`, `VALIDATED`, `REGRESSED`, or `CLOSED`.
An implemented issue needs three later successful natural runs without recurrence to become
`VALIDATED`; recurrence becomes `REGRESSED`.

The only automatic remediation is atomic regeneration of the derived history index. Source,
configuration, Git, prompts, budgets, schedules, services, research, providers, ledgers, safety
evidence, scoring, risk, Buy gates, and broker/account/order surfaces are not allowlisted.

## Deterministic commands

```bash
python3 scripts/morning_finality_guard.py --target YYYY-MM-DD
python3 scripts/morning_audit_supervisor.py self-check
python3 scripts/morning_audit_supervisor.py run --target YYYY-MM-DD --trigger watchdog
python3 scripts/morning_engineering_audit.py rebuild-history-index
python3 scripts/test_morning_audit_supervisor.py
python3 scripts/test_morning_engineering_audit.py
```

Do not invoke the production audit as a historical backfill. Tests and the August 10 replay use
isolated fixture roots. The next naturally scheduled Morning is the production validation event.
