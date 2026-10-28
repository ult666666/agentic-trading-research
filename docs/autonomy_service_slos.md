# Autonomous Workflow Service SLOs

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

A loaded, byte-matching LaunchAgent proves configuration identity, not that a workflow started or
finished on time. `config/autonomy_service_slos.json` adds deterministic service objectives for
the five research workflows:

| Workflow | Scheduled PT | Start by PT | Expected terminal by PT |
|---|---:|---:|---:|
| Intake | 05:00 | 05:05 | 05:20 |
| Macro actual refresh | 05:35 | 05:40 | 05:45 |
| Morning | 05:38 | 05:43 | 06:15 |
| Post-open | 06:45 | 06:50 | 07:05 |
| Post-close | 13:15 | 13:20 | 14:15 |

The operating timezone is `America/Los_Angeles`. `scripts/lib/tzutil.py` and `zoneinfo` derive UTC
instants for the target session date, including DST. The SLO policy is cross-locked to
`config/autonomy_workflows.json` and validated against
`schemas/autonomy_service_slo_policy.schema.json`.

## Evidence Sources

Start times and non-Morning terminal times come from structurally validated attempt records in
`data/runtime/autonomy/<target>.json`; post-close additionally requires the receipt for its exact
mode:

- `started_utc` establishes the start objective;
- `finished_utc` is accepted only when the workflow status satisfies the policy;
- a degraded intake/post-close terminal is valid for timeliness because workflow quality is
  reported separately; post-close is terminal-eligible only when its exact run-scoped
  `terminal_receipt.json` revalidates against the report, cutoff envelope, and autonomy state;
- `post_close skipped / no_official_morning_artifact` is terminal-eligible only when the exact
  run-scoped `post_close_maintenance_receipt/1.0` revalidates its supervised outer run, canonical
  Morning absence, ordered 20-stage invocation results (never mislabeled as artifact
  completion), explicit present/absent output observations, self hash, protected before/after equality,
  zero narrative-finality files, and final state/postcondition hash linkage. A missing, tampered,
  forged, stale, collision-produced, or currently contradicted receipt is `at_issue` and misses
  the SLO. This receipt never substitutes for a report terminal in natural-run acceptance;
- post-open `no_official_morning_artifact` is an expected deterministic skip only when the strict
  official-artifact resolver confirms that neither canonical v1 nor receipt-bound governance-v2
  report exists; a malformed/ambiguous dependency or a skip contradicted by an accepted official
  report is surfaced as an issue;
- a completed/degraded post-open attempt is terminal-eligible only when its exact run-scoped
  `post_open_terminal_receipt/1.0` revalidates the immutable report, deterministic market snapshot,
  current Morning report+receipt dependency, writer/finalizer outcome, and safety flags; missing or
  tampered finality is `at_issue`, never a met objective;
- failed, blocked, unexpected-skip, malformed, future, reversed, or missing timestamps never
  become a met objective.

Morning remains stricter. Its terminal time comes from either a hash-validated
`research_freeze_receipt/1.0`, or the deterministic no-live fallback's
`deterministic_terminal_receipt/1.0`. The latter is accepted only for
`PRELIMINARY_PRICE_UNAVAILABLE` in the official window and must bind the exact report and canonical
preflight hashes while retaining zero candidates, no metrics, no Buy, and no execution authority.
A bare Markdown file, model completion timestamp, wrapper exit code, or Preliminary timeout prose
cannot satisfy the 06:15 objective. A present but invalid freeze receipt is never bypassed by the
fallback receipt.

The market calendar is mandatory. A covered non-trading day makes every SLO `not_applicable`.
Missing calendar coverage or an invalid verdict makes the policy surface and every workflow SLO
`invalid`.

## Status Semantics

Each workflow reports independent `start_status` and `terminal_status`:

- `met`: the validated event occurred at or before its deadline;
- `pending`: the deadline has not passed and no final event exists;
- `missed`: the deadline passed, the event was late, or the workflow reached a non-contract
  terminal;
- `not_applicable`: covered non-trading day;
- `invalid`: policy, timestamp, calendar, receipt, or semantic validation failed.

The aggregate workflow `status` is invalid first, then missed, then pending, otherwise met. Exact
start and terminal lateness are reported in seconds. These are reliability metrics, never trading
performance or profitability metrics.

## Automatic Refresh

After every supervised terminal path, `scripts/autonomy_orchestrator.py` launches exactly one
bounded local refresh:

```text
python autonomy_service_health.py
  --target <target>
  --root <repository>
  --autonomy-state <exact state path>
  --now <terminal UTC>
  --out data/runtime/health/<target>_autonomy_service_health.json
```

The invocation uses an argument list with no shell, closed stdin, a 30-second timeout, and the
supervisor's control-only environment, which removes provider credentials and dynamic trading
variables. Exit 0 (healthy) and exit 1 (valid `at_issue`) both mean a fresh artifact was written.
Exit 2, timeout, missing script, malformed output, stale timestamp, or a bad artifact hash is
reported in `service_health_refresh` but never changes the original workflow state, outcome, or
exit code.

This refresh pattern means a later workflow can expose an earlier missed workflow. For example, if
the sleeping Mac misses intake but wakes for Morning, Morning's terminal refresh will show the
missing intake objective.

## Commands

Validate the policy and evaluator:

```bash
python3 scripts/test_autonomy_service_slo.py
python3 scripts/test_autonomy_service_health.py
```

Inspect a target without invoking a model or broker:

```bash
python3 scripts/autonomy_service_health.py --target YYYY-MM-DD
```

Exit 0 means the complete composite health surface is not at issue. Exit 1 means a valid local
receipt was written with one or more operational issues. Exit 2 is structural failure.

The composite includes a diagnostic `research_artifact_health` plane. It validates the current
candidate-health projection and reuses the supervisor's exact official-report resolver, so an
invalid candidate bundle or contradictory post-open skip cannot coexist with an overall green
result. This plane is operational evidence only and does not alter the SLO event timestamps.

## Safety Boundary

The SLO plane is read-only, diagnostic-only, and local-artifact-only. It has no retry, schedule,
report, strategy, risk, source, Buy, broker, account, order, or execution authority. It cannot
start a missed workflow or repair an incident. No account data, credentials, market payload, model
output, price, position, or order details enter the policy or refresh result.
