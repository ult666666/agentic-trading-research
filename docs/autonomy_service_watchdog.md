# Autonomy Service Watchdog

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: deterministic local observer for the five service-SLO terminal deadlines, plus one fixed
zero-attempt BLS macro continuity dispatch before the 05:46 evaluation. It has no trading or retry
authority.

Authority: `AGENTS.md` overrides this document.

## Why It Exists

Each supervised workflow refreshes composite service health when it reaches a terminal state.
That cannot expose a LaunchAgent that never starts. The independent watchdog closes this
observability gap. For macro refresh only, it also closes the safe recovery gap: during 05:46-05:48
PT it may dispatch the exact allowlisted BLS runner only when the autonomy state has zero prior
macro attempts. It never invokes a model, broker/account surface, order path, or dynamic command.

The exact PT checkpoints are versioned in
`config/autonomy_service_alerts.json` and cross-locked to
`config/autonomy_service_slos.json`:

| Workflow | Terminal objective | Watchdog checkpoint |
|---|---:|---:|
| Intake | 05:20 | 05:21 |
| Macro actual refresh | 05:45 | 05:46 |
| Morning | 06:15 | 06:16 |
| Post-open | 07:05 | 07:06 |
| Post-close | 14:15 | 14:16 |

The LaunchAgent uses all five `StartCalendarInterval` entries. It does not use a loop, sleep,
dynamic schedule, or model-driven timer.

## Evaluation Flow

`scripts/run_autonomy_watchdog.sh` performs one bounded
local pass:

1. derive the PT target date with `scripts/lib/tzutil.py`;
2. skip a validated non-trading day;
3. require the critical integrity manifest and strict read-only broker contract;
4. validate the service-alert policy and its one-minute SLO cross-lock;
5. request `recover-macro --trigger watchdog`; outside 05:46-05:48 this is a write-free no-op;
6. when in-window, atomically require zero prior macro attempts, repeat all ordinary workflow
   safety/dependency gates, apply a final dispatch-time cutoff, and cap the child at 90 seconds;
7. regenerate `data/runtime/health/<target>_autonomy_service_health.json`;
8. evaluate the new health receipt under an `fcntl` state lock;
9. atomically write the self-hashed state and decision receipt;
10. translate only a new/changed incident or recovery exit code into fixed local macOS text.

The 05:45 macro service objective is never repaired retroactively. A 05:46 recovery can make the
input available for Morning while service health continues to report the original late/missed SLO.
Natural-run metrics remain unchanged. If a macro attempt is already running, completed, degraded,
skipped, failed, or blocked, recovery does nothing. A delayed ordinary LaunchAgent that races a
live recovery still follows the normal fail-closed lock-contention path; it is not retried.
The same no-retry rule covers dead-owner and structurally inconsistent state: only a pristine
`pending`/zero-attempt/null-latest record may start an ordinary child. Coherent terminals are
preserved, running state returns contention, and ambiguous state fails structurally without a write.

The target output is also one-shot under the runner lock. A valid direct/manual receipt is reused
byte-for-byte without another BLS request. Invalid, target-mismatched, or symlinked output is never
overwritten and fails closed. Immediately before an ordinary child dispatch, the current PT phase
is checked again so slow preflight gates cannot carry old eligibility beyond 05:49:59.

There is one exact no-dispatch handoff to Morning. A watchdog recovery can reserve the attempt while
its gates run, then discover at the final check that 05:48 PT has passed. If and only if that is the
sole prior macro history, every ordinary safety/dependency gate passed, the terminal is the exact
after-watchdog-window skip, and there is no child PID, runner event, postcondition, or ambiguous
metadata, Morning may append one bounded recovery attempt. Regular and early-close trading-day
reasons are accepted; no other market-day reason is. The skipped watchdog record remains in the
append-only attempt/event history and the missed 05:45 SLO remains missed. The prior attempt hash,
daily-state configuration hash, and current validated configuration hash must match exactly.

The evaluator itself never sends a notification. The wrapper does not include health content in
the notification, so market data, account data, paths, and untrusted text cannot enter the message.
Notification delivery is best-effort and is not treated as confirmed.

On an integrity-manifest gate failure the wrapper additionally raises the durable
operator-attention marker (`data/runtime/health/attention_required.json` plus
`ATTENTION_REQUIRED.md`, reason `integrity_manifest_failed`) before exiting fail-closed, and
clears exactly that reason at the next tick where the gate passes. The Layer-4 sentry
escalates any present marker through its O9 contract at every subsequent tick, so a
fail-closed day can no longer pass silently. The ephemeral banner remains best-effort; the
marker is the durable escalation.

## Alert Semantics

One local alert is requested for:

- any workflow service-SLO status of `missed`;
- any workflow service-SLO status of `invalid`;
- an invalid SLO policy;
- scheduler configuration that is not `healthy`;
- a malformed, target-mismatched, older-than-120-seconds, or materially future-dated composite
  health receipt.

The evaluator is deliberately silent for:

- `pending`, `met`, and `not_applicable` service objectives;
- an unchanged active incident;
- the independent Mac power-readiness issue by itself;
- missing/unreadable health in direct evaluator use, where the prior incident state is preserved.

An added or removed condition changes the incident fingerprint and requests one new alert. The
target session date is part of the fingerprint, so a repeated problem on the next trading day is a
new incident. A valid clear state after an incident requests one recovery notification. Further
clear checks remain silent.

Exit codes from `autonomy_service_alert.py` are:

- `0`: clear, unchanged, or unavailable without changing the prior incident;
- `10`: new or changed alert;
- `11`: recovery;
- `2`: policy, state, receipt, or other structural failure.

The wrapper maps structural health/evaluator failure to a fixed local operator warning and exits
nonzero. A failed recovery remains visible to the subsequent health pass and is never retried.

## Artifacts

- `data/runtime/health/autonomy_service_alert_state.json`: cross-session deduplication state;
- `data/runtime/health/autonomy_service_alert.json`: latest alert decision receipt;
- `data/runtime/health/<target>_autonomy_service_health.json`: validated input receipt;
- `logs/automation.log` and `logs/errors.log`: local runner diagnostics.

State and receipt writes are atomic and self-hashed. A malformed or tampered existing state blocks
evaluation and is not overwritten. The artifacts reject credential, account, price, position,
balance, and order-shaped fields.

## Commands

Validate the policy, schemas, and exact one-minute SLO cross-lock without writing runtime state:

```bash
python3 scripts/autonomy_service_alert.py --check
```

Inspect the narrow LaunchAgent plan:

```bash
bash scripts/install_launchd.sh --plan --only watchdog
```

Install or reload only the watchdog (mutates `~/Library/LaunchAgents`; does not kickstart it):

```bash
bash scripts/install_launchd.sh --confirm --only watchdog
```

Inspect repository/installed/loaded agreement for all expected labels:

```bash
python3 scripts/launchd_scheduler_health.py
```

Run focused tests:

```bash
python3 scripts/test_autonomy_service_watchdog.py
python3 scripts/test_launchd_scheduler_health.py
```

## Safety and Limits

The watchdog's alert evaluation is diagnostic-only. Its sole workflow surface is the exact,
time-gated, zero-prior-attempt BLS macro recovery described above. That collector uses official
public macro data and writes only the normal hash-bound local receipt. The watchdog cannot classify
a candidate, edit a report, satisfy a freeze gate, increment research metrics, activate a strategy,
alter risk limits, access an account, or preview/simulate/place/modify/cancel an order. It has no
model, broker, external-message, retry, schedule-change, or dynamic-runner surface.

It cannot run while the Mac is powered off or asleep. Machine wake readiness remains a separate
manual operating prerequisite documented in
[`docs/power_schedule_health.md`](power_schedule_health.md). Local notification delivery also
depends on macOS notification settings; the durable receipt is the audit source.

Rollback is narrow: unload/remove only `com.charles.trading.watchdog` through the installer. The
five supervised workflows, their states, reports, ledgers, strategy controls, and broker safety
remain unchanged.
