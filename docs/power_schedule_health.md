# Mac Power-Schedule Readiness

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

The deterministic data intake is scheduled for 05:00 PT. A healthy LaunchAgent does not prove
that a sleeping or powered-off Mac will be awake when launchd needs to start it. This control
adds a separate, read-only machine-power plane:

- workflow start: 05:00 PT on `MTWRF`;
- required repeating wake/power-on: no later than 04:55 PT;
- minimum lead: 5 minutes;
- operating timezone: `America/Los_Angeles`.

The policy is versioned in `config/power_schedule.json`. The normalized receipt schema is
`schemas/power_schedule_health.schema.json`.

## Read-Only Inspection

`scripts/power_schedule_health.py` invokes exactly:

```bash
/usr/bin/pmset -g sched
```

It does not use a shell, `sudo`, or any mutating `pmset` operation. Only repeating events are
parsed. One-off scheduled events are ignored. Raw command output is SHA-256 hashed and discarded;
the receipt retains normalized event type, PT time, weekday coverage, readiness flags, and the
hash.

Accepted repeating event types are `wakeorpoweron` and the normalized macOS output form
`wakepoweron`. An event is ready only when it covers every required weekday and occurs at or
before 04:55 PT.

Statuses:

- `ready`: a qualifying repeating event meets the deadline;
- `at_issue`: the inspection succeeded but the event is missing, late, wrong-type, or lacks
  weekday coverage;
- `unavailable`: `pmset -g sched` could not be inspected;
- `invalid`: a consuming health surface rejected the receipt or policy.

The self-hash is not trusted by itself. Validation recomputes event flags, selection, lead,
lateness, status, reasons, remediation semantics, and all no-authority safety fields.

## Operational Integration

On a valid trading day, `scripts/run_data_intake.sh` validates the policy before its integrity and
broker gates. A malformed power policy blocks intake as a contract error. A valid live result that
is `at_issue` or `unavailable` writes/logs the diagnostic, emits fixed local-only notification
text, and lets an already-awake catch-up intake continue. Power readiness never changes data-lake
authority or a research classification.

`scripts/install_launchd.sh --plan|--status|--confirm` runs the read-only inspection whenever the
selected labels include intake. It prints the administrator remediation but never executes it.
The composite `scripts/autonomy_service_health.py` surface reports a reduced
`power_schedule_health` projection without raw `pmset` output. A provided non-ready receipt forces
overall operational health to `at_issue` while remaining independent of scheduler configuration.

## Verification and Manual Remediation

The accepted machine state is dynamic. First inspect the existing repeating schedule:

```bash
pmset -g sched
```

Then run the policy-locked administrator command:

```bash
sudo pmset repeat wakeorpoweron MTWRF 04:55:00
```

macOS requires an administrator password. The agent cannot and must not supply or bypass it.
`pmset repeat` owns the machine's single pair of repeating power events, so review the pre-change
output before approving the replacement if another repeating power-off event is present.

Verify after the command:

```bash
pmset -g sched
python3 scripts/power_schedule_health.py
```

The second command must exit 0 with `status=ready`, `time_pt=04:55`, `lead_minutes=5`, and
`minutes_after_required=0`. The July 12 repair reached that state, but each future readiness pass
rechecks it rather than trusting the historical observation.

## Continuity Layer

`com.charles.trading.continuity` is a separate weekday-only LaunchAgent. At 04:56 PT it validates
the real target date, skips market holidays, starts a bounded `caffeinate -i -s` process through
07:10 PT, and runs `scripts/next_session_readiness.py`. Inspect it with:

```bash
bash scripts/install_morning_continuity.sh --status
```

This closes ordinary idle-sleep gaps between wake, intake, Morning, and post-open. It cannot defeat
lid-close sleep, shutdown, battery exhaustion, power loss, or an external-provider outage.

## Safety Boundary

This plane is diagnostic only. It grants no source authority, Buy authority, scoring authority,
strategy activation, broker access, account access, or execution capability. It invokes no model
and costs zero dollars. Rollback means restoring the pre-change repeating event with an explicitly
reviewed administrator command; no strategy, risk, report, ledger, or broker setting is involved.
