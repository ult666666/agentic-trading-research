# Autonomous Research Orchestration

Status: framework implemented and activated 2026-07-09. As of 2026-07-16, the five workflow
LaunchAgents and the independent service-SLO watchdog are loaded and byte-match their repository
contracts. The 2026-07-10 natural intake completed degraded, the
Morning run failed safely on its model timeout after missing the 06:15 PT service objective, and
post-open skipped as required because no Official Morning artifact existed. Configuration health
therefore remained healthy while that daily workflow/service result was at issue. The July 12
stabilization reduced the bounded model path, pinned fast workers, repaired virtual-target
rehearsal isolation, and added pre-intake readiness plus wake continuity; the next natural session
is still the authority on whether those changes meet the SLO.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. The framework orchestrates
research workflows only. It cannot preview, simulate, place, modify, or cancel an order; cannot
activate a strategy; and cannot change protected risk limits.

## Purpose

The existing runners already contain the hard domain logic:

- [`run_data_intake.sh`](../scripts/run_data_intake.sh): point-in-time data collection,
  immutable lake snapshot, and readiness receipt without a model;
- [`run_macro_actual_refresh.sh`](../scripts/run_macro_actual_refresh.sh): 05:35 BLS-only
  post-release observation refresh and deterministic Buy blocker without a model;
- [`run_morning_scan.sh`](../scripts/run_morning_scan.sh): data authority, research ensemble,
  draft-only audited model, run/content-bound freeze request, trusted outer publisher,
  deterministic Buy/freeze gates, receipt re-anchoring, singleton reconciliation, and health;
- [`run_premarket_refresh.sh`](../scripts/run_premarket_refresh.sh): post-open validation;
- [`run_post_close_review.sh`](../scripts/run_post_close_review.sh): outcome settlement,
  calibration, model monitoring, unattended quant-research governance, and post-close narrative.

The supervisor deliberately does not copy or replace those rules. It adds the missing common
control plane around them:

```text
LaunchAgent / manual invocation
        |
        | TRADING_AUTONOMY_ENABLE=1
        v
existing runner -> delegate_to_autonomy()
        |
        v
autonomy_orchestrator.py
  1. validate immutable workflow contract
  2. open/update target-date state under a file lock
  3. validate US trading day
  4. verify the approved critical-file integrity manifest
  5. validate strict read-only broker permissions
  6. apply workflow dependency and session-time gates
  7. invoke one exact allowlisted runner, shell=False, no supervisor retry
  8. bound and terminate the whole process group on timeout
  9. validate the runner's fresh state and existing artifact contract
 10. write one terminal control-plane state
        |
        v
same runner with TRADING_AUTONOMY_CHILD=1 (no recursion)
```

## Files

- [`config/autonomy_workflows.json`](../config/autonomy_workflows.json): versioned workflow,
  schedule, timeout, dependency, artifact, and safety contract.
- [`scripts/autonomy_orchestrator.py`](../scripts/autonomy_orchestrator.py): fail-closed
  supervisor and state writer.
- [`scripts/post_close_maintenance_receipt.py`](../scripts/post_close_maintenance_receipt.py):
  exclusive-create, self-hashed evidence for exact-absence deterministic maintenance; it has no
  narrative-finality or natural-run authority.
- [`scripts/test_autonomy_orchestrator.py`](../scripts/test_autonomy_orchestrator.py): isolated
  config/state/gate/artifact/subprocess tests.
- [`scripts/autonomy_service_health.py`](../scripts/autonomy_service_health.py): read-only
  reconciler that reports scheduler configuration, workflow outcome, and service-SLO health as
  separate planes.
- [`scripts/test_autonomy_service_health.py`](../scripts/test_autonomy_service_health.py):
  hermetic July-10 timeout, validated-freeze deadline, hash, and safety-contract tests.
- [`scripts/autonomy_service_alert.py`](../scripts/autonomy_service_alert.py): self-hashed,
  deduplicated local alert-state evaluator for missed/invalid service objectives.
- [`scripts/run_autonomy_watchdog.sh`](../scripts/run_autonomy_watchdog.sh): independent
  deterministic observer invoked one minute after each terminal deadline, with one fixed
  zero-attempt macro continuity dispatch before its 05:46 evaluation; see
  [`docs/autonomy_service_watchdog.md`](autonomy_service_watchdog.md).
- [`scripts/release_snapshot.py`](../scripts/release_snapshot.py): non-destructive committed/
  staged/unstaged/untracked recovery bundle and current-state provenance gate; see
  [`docs/release_runbook.md`](release_runbook.md).
- [`scripts/broker_runtime_audit.py`](../scripts/broker_runtime_audit.py) and
  [`scripts/audited_claude.py`](../scripts/audited_claude.py): hash-only runtime broker-attempt
  receipts and PreToolUse denial; see
  [`docs/broker_runtime_safety.md`](broker_runtime_safety.md).
- [`scripts/freeze_request.py`](../scripts/freeze_request.py) and
  [`scripts/model_integrity_binding.py`](../scripts/model_integrity_binding.py): read-only
  revalidation of the exact same-run model artifacts, wrapper result, broker event chain, and
  control-plane integrity receipt before the Morning publisher becomes reachable.
- [`config/morning_dag.json`](../config/morning_dag.json) and
  [`scripts/morning_dag.py`](../scripts/morning_dag.py): disabled-by-default deterministic shadow
  replacement for the monolithic Morning model session; see [`docs/morning_dag.md`](morning_dag.md).
- [`scripts/lib/common.sh`](../scripts/lib/common.sh): opt-in delegation helper.
- [`scripts/next_session_readiness.py`](../scripts/next_session_readiness.py): redacted pre-session
  market-day, integrity, scheduler, power, model, connector, timing, and clean-slot gate.
- [`scripts/run_morning_continuity.sh`](../scripts/run_morning_continuity.sh): weekday-only bounded
  04:56-07:10 wake-continuity layer; installed independently from the five workflows and watchdog.
- `data/runtime/autonomy/<target>.json`: untracked daily control-plane state.

## Hard Safety Contract

The configuration is accepted only when all of these are exact:

- `research_only=true`;
- `execution_enabled=false`;
- `broker_access=strict_read_only`;
- the critical-file integrity manifest is mandatory;
- no subprocess shell or dynamic command;
- one supervisor attempt and no supervisor retry; macro continuity can create only the missing
  first attempt and atomically refuses any prior running/terminal/failed attempt;
- no strategy activation or risk-limit authority;
- five exact supervised runner paths, labels, schedules, phase gates, dependencies, and artifact
  contracts. The watchdog has its own exact five-checkpoint contract and may invoke only the
  allowlisted `recover-macro --trigger watchdog` path during 05:46-05:48 PT.

Before every child runner, the supervisor first runs `scripts/build_manifest.py --check`, then
invokes
[`validate_broker_permissions.py`](../scripts/validate_broker_permissions.py). A failure or timeout
in either check blocks the child with exit 78. This adds common integrity and broker gates to
post-open and post-close without removing the morning runner's own mandatory gate.

The child environment is allowlisted. Provider keys, tokens, cookies, authentication fields, and
other secret-like environment variables are not inherited. Existing runners continue to load
approved provider credentials from the git-ignored secret file only inside their deterministic
collector window.

The state artifact stores status codes, paths, hashes, timestamps, and process IDs only. It never
stores prices, portfolio values, positions, account identifiers, raw source data, model output, or
credentials.

## Workflow Contracts

### Data intake

- Schedule contract: 05:00 PT; allowed supervisor window 04:45-05:30 PT.
- No model and no broker-data tool is invoked.
- New commercial providers remain disabled; public/local adapters are capability-bounded.
- Postcondition: one fresh, self-hashed `data_lake_readiness/1.0` receipt.
- A fail-soft, no-network provider-bake-off observer writes aggregate `collecting/blocked/quality`
  receipts after intake; it cannot affect the data-lake postcondition or production authority.
- `degraded` is a valid observable terminal state; malformed or inconsistent state is a failure.

### Morning

- Schedule contract: 05:38 PT; start SLO 05:43 PT.
- Before its child takes the repository runner lock, the supervisor requests the same zero-attempt
  macro recovery during 05:49-05:52 PT. This covers a Mac that also missed the 05:46 watchdog; it
  is bounded to 90 seconds and never retries a prior macro attempt.
- The Morning attempt records continuity execution as `completed`, `no_dispatch`, or `at_issue`
  separately from a fresh validation of the target macro receipt. `input_available` is never
  inferred from a zero return code, and `slo_repair_claimed` remains false.
- Session legality remains delegated to `stage_decision.py` and the freeze gates.
- If the direct-HTTP primary is not fresh and the read-only Robinhood collector also supplies no
  validated live quote, `deterministic_no_live_terminal.py` writes a hash-bound
  `PRELIMINARY_PRICE_UNAVAILABLE` artifact and bypasses the main research model and ensemble. The
  collector separately discloses whether its restricted low-cost model process was invoked.
- Postcondition: exactly one accepted terminal report for the run, using
  `terminal_reconcile.py` in check-only mode.
- Duplicate or zero terminal artifacts turn a clean child exit into a supervisor failure.

### Macro actual refresh

- Schedule contract: 05:35 PT; allowed supervisor window 05:30-05:49:59 PT.
- Depends on the exact validated target-date 05:00 data-lake readiness receipt.
- Fetches only mapped BLS series for official calendar events due by the refresh cutoff.
- Postcondition: one fresh, self-hashed `macro_actual_refresh/1.0` receipt. `ready` and
  `no_due_events` are healthy; `degraded` is an honest terminal that blocks Buy; malformed,
  missing, or source-ambiguous receipts fail.
- A missing scheduled attempt gets two narrow continuity opportunities: 05:46-05:48 from the
  watchdog and 05:49-05:52 from the Morning supervisor before its child starts. Both require zero
  prior attempts atomically, re-run the normal trading-day/integrity/read-only/dependency gates,
  apply a final just-in-time cutoff, and cap the macro child at 90 seconds.
- Recovery after 05:45 remains an honest service-SLO miss; it improves the same-session research
  input but never rewrites timeliness or natural-run metrics. After 05:52 it is a write-free no-op.
- Immediately before an ordinary macro child starts, production re-checks the current PT phase;
  invocation-time eligibility cannot survive slow integrity or permission gates past 05:49:59.
- The Morning child retains its deterministic artifact fallback only when no valid target receipt
  exists, and fingerprints the result before any model starts.
- Macro supervision is one-shot. Except for the exact no-dispatch handoff below, a child can start
  only when attempts are empty, state is `pending`, and `latest_run_id` is null. Recovery seeing
  any prior attempt returns a truthful no-dispatch observation. An ordinary duplicate preserves a
  coherent terminal, returns lock contention for running state, and treats every inconsistent
  state/attempt/latest-id combination as an at-issue structural error without mutating history.
- Direct/manual and supervised runners share the repository lock and the target receipt is
  idempotent under that lock. A valid existing receipt is byte-preserved without a provider call;
  an invalid, target-mismatched, symlinked, or otherwise ambiguous existing output is also
  preserved but fails closed. A runner may accept an older valid receipt only when its fresh task
  detail explicitly attests this byte-preserving path.
- One narrower handoff prevents a reservation from suppressing useful work: if the *only* prior
  macro attempt is a production watchdog recovery whose ordinary trading-day (regular or early
  close), integrity, strict-read-only, readiness, and initial phase gates all passed, but whose
  final dispatch gate crossed 05:48 PT before any child PID or `runner_started` event existed,
  Morning may append one recovery attempt. The prior attempt, daily state, and current workflow
  configuration hashes must be identical. The prior skipped attempt and its events are preserved.
  Exact attempt/preflight/event shapes, timestamps, IDs, and hashes are required; any blocked,
  failed, running, dry-run, child-bearing, before-window, ordinary, extra, or ambiguous history
  remains non-retryable.

### Post-open

- Schedule contract: 06:45 PT.
- Earliest start: 06:45 PT; latest: before that session's close.
- Requires exactly one accepted official artifact: either the canonical v1 report at
  `reports/morning/<target>.md`, or the canonical governance-v2 report named by a valid
  `research_freeze_receipt/1.0`. The v2 receipt must match the target, carry an
  `OFFICIAL_RESEARCH_*` classification with `official=true` and `execution_enabled=false`, point
  to the canonical report slot, and match the report SHA-256.
- If neither official slot exists, the supervisor performs a deterministic clean skip and saves
  model usage. A Preliminary does not satisfy the dependency; a malformed, hash-mismatched, or
  ambiguous dual-slot state fails closed instead of being reported as a clean skip.
- The supervisor revalidates the selected artifact immediately before child launch and passes its
  exact path and kind through `TRADING_ACCEPTED_MORNING_REPORT` and
  `TRADING_ACCEPTED_MORNING_KIND`. The prompt may not guess from newest files or append to a frozen
  report.
- Before the model, the runner writes an immutable run intent under
  `data/runtime/post_open_runs/<target>/<run-id>/intent_receipt.json`. It binds the exact
  deterministic market snapshot, the fully revalidated Morning report and Morning receipt, and
  the complete pre-run inventory of target-day post-open reports.
- A zero model exit, filename, or mtime is never a successful terminal. Exactly one new
  `reports/intraday/<target>-postopen*.md` file must appear without changing any prior report.
  `post_open_finality.py` then writes an immutable `terminal_receipt.json` binding the new report
  bytes, snapshot bytes, Morning dependency bytes, writer exit, finalizer outcome, and research-
  only safety flags. The supervisor, health service, and natural-run acceptance each dynamically
  revalidate this receipt and its Morning dependency before accepting a completed run.

### Post-close

- Schedule contract: 13:15 PT.
- Cannot start before the actual regular/early-close time derived from the market-day artifact.
- Morning state selects one of two non-interchangeable supervised modes. A fully revalidated
  Official Morning artifact keeps the existing narrative path unchanged. The exact canonical
  absence tuple `(path=None, kind=none, reason=no_official_morning_artifact)` admits only the
  fixed deterministic maintenance path. A Preliminary grants no narrative authority; when both
  canonical Official slots are lexically absent it therefore routes to maintenance only. A
  legacy-only slot, unbound prose file, malformed
  receipt, ambiguous slot, resolver error, or authority change between either outer check and
  the child check blocks with exit 78 before diagnostic work.
- Exact absence no longer suppresses useful outcome/data-health work. Maintenance runs the same
  ordered 20 stage invocations already present before the narrative writer (return codes are
  recorded separately from output presence and do not claim artifact completion): post-close market snapshot,
  outcome-bar archive, rejection record/outcomes, outcome tracker, attribution, calibration,
  outcome health, free-source health/alert, candidate and SEC rollups, manual-observation sync and
  health, model-risk monitor, quant capture/corpus/grid/factory/stager. Stage return codes are
  observations; they never mint report, Buy, risk, governance, strategy, broker, or execution
  authority.
- Before stage 1, maintenance snapshots the target post-close-report namespace, Watchlist,
  protected risk/governance/source configuration, and the run's narrative cutoff/terminal paths.
  After stage 20 all protected bytes must be unchanged and both narrative-finality paths must
  remain absent. It then exclusive-creates
  `data/runtime/post_close_runs/<target>/<run-id>/maintenance_receipt.json`, binding the exact
  outer run/start/child, absence reason, diagnostic run ID, ordered stage dispositions, output
  observations, protected before/after hashes, network-attempt observation, zero-authority safety
  flags, and its own hash.
- A valid maintenance receipt maps the outer attempt to
  `skipped / no_official_morning_artifact`; this is a truthful no-report terminal, not a failed
  diagnostic run. A bare skip, invalid receipt, protected drift, TOCTOU, or receipt collision is
  `at_issue`/blocked. Direct unsupervised exact absence still skips without running maintenance.
- Maintenance exits before Claude authentication, journal reconciliation, report contract,
  model selection/invocation, repair, Watchlist/report writer, cutoff capture, and terminal seal.
  Its receipt is never loaded by natural-run acceptance, which remains report-only and requires
  the narrative `terminal_receipt.json`.
- Before the narrative model, the runner invokes the offline research factory. It validates the
  target-date readiness receipt and any pre-registered experiment bundles, then writes
  `data/runtime/health/<target>_research_factory.json`. Data-waiting and evidence-rejected states
  are valid terminals; structural failure is fail-soft for the narrative but never grants strategy
  authority. See [`docs/research_factory.md`](research_factory.md).
- Immediately before that independent evaluation, the runner captures an immutable daily feature
  package, rebuilds the forward corpus, and invokes the complete pre-registered grid worker. All
  three are deterministic/local and return quickly when readiness or sample maturity is absent.
- After factory evaluation, the current-run strategy-artifact stager requires the grid and factory
  health records to agree on the exact candidate bundle before creating or revalidating a local
  write-once comparison package. A valid rejected/data-waiting bundle becomes `not_candidate`.
  Staging never mutates the registry or grants promotion, strategy, broker, or execution authority.
  See [`docs/strategy_artifact_staging.md`](strategy_artifact_staging.md).
- In narrative mode, the report first passes `post_close_report_contract/1.1`, which truthfully
  binds the exact
  supervised run while it is still `running (non-terminal)` at the report cutoff. Before the
  child returns, `post_close_finality.py capture-cutoff` publishes an exclusive-create,
  self-hashed envelope at
  `data/runtime/post_close_runs/<target>/<run-id>/cutoff_receipt.json`.
- Only after the supervisor has durably written the terminal attempt does it publish the linked
  `terminal_receipt.json`. That receipt revalidates the report, cutoff envelope, state safety,
  single-attempt invariant, event chain, timestamps, outcome mapping, and hashes. Missing or
  conflicting finality makes health and natural-run acceptance fail closed; the report itself is
  never rewritten to claim its later result.
- Post-close supervision is one-shot for a target. A coherent terminal is preserved as a clean
  duplicate no-op, a live attempt returns 75, and malformed/ambiguous history returns 2. No path
  may recover a dead owner by launching a second post-close child.

## State Model

One state file represents the target session and all five workflows:

```text
pending -> running -> completed | degraded | skipped | failed | blocked
```

Each attempt carries an opaque run ID, owner/child PID, start/finish UTC, dry-run flag, gate
summary, child exit code, postcondition result, terminal outcome, and the exact configuration
SHA-256 used for that attempt. The file is atomically
replaced under an `fcntl` lock.

If a second supervisor finds a live owner PID, it exits 75 and does not launch a child. For lanes
that are not one-shot, if the recorded owner is dead the earlier attempt is preserved as
`stale_owner_recovered` before a new
attempt starts. A malformed state file blocks the workflow and is never overwritten.

Daily `overall_state` is derived only from workflow states:

- `pending`: nothing has run;
- `running`: at least one workflow is active;
- `in_progress`: at least one workflow is terminal and later sessions remain pending;
- `complete`: all workflows are terminal without a failed/blocked state;
- `at_issue`: any workflow failed or was blocked.

These are operational states, never trading-performance labels.

## Multi-Plane Health

`launchd_scheduler_health.py` answers one narrow question: do the repository plist, installed
plist, and loaded service identity agree? Its `ok` field is retained for compatibility but now
declares `health_scope=scheduler_configuration_only`; a nonzero last child exit does not make the
configuration drifted.

`autonomy_service_health.py` combines that configuration result with the immutable daily autonomy
state, the Morning run-status artifact, a hash-validated freeze receipt, the broker runtime SLI,
the live runtime-provenance gate (including its separate exact-snapshot forensic result), the
validated aggregate free-source health receipt, and a
read-only machine power-schedule inspection. It reports:

- `scheduler_config_health`: loaded/configured identity only;
- `workflow_health`: completed/degraded/skipped/failed/blocked state, with `failed_safe` reserved
  for a failed Morning whose run-status proves no broker mutation, no metric/ledger increment, and
  a Preliminary timeout checkpoint;
- `service_slo_health`: independent start and expected-terminal objectives for intake, Morning,
  post-open, and post-close. Intake/post-open/post-close use validated supervisor attempts;
  Morning still requires the hash-validated canonical freeze receipt. Exact deadlines, sources,
  and lateness are versioned in `config/autonomy_service_slos.json`.
- `research_artifact_health`: diagnostic projection of the frozen candidate artifact and strict
  official-report resolver. Invalid/missing candidate artifacts, invalid candidate-health
  receipts, an invalid report dependency, or a recorded `no_official_morning_artifact` skip that
  contradicts an accepted official report make the composite surface `at_issue`.
- `power_schedule_health`: whether a repeating accepted Mac wake/power-on event covers `MTWRF`
  and occurs by 04:55 PT, five minutes before intake. It reports only normalized event metadata,
  lead/lateness, reasons, and the manual remediation; raw `pmset` output is discarded.
- `foundation_safety.broker_runtime`: zero-verified / unobservable / violation runtime attempts;
- `foundation_safety.release_provenance`: two conclusions that must not be collapsed. `status` /
  `release_reproducible` retain the exact private-snapshot forensic comparison, while
  `runtime_binding_status` / `runtime_authority_passed` decide whether committed runtime source is
  still bound. Only exact `match` or validator-approved `expected_output_drift` passes; staged,
  protected-source, executable-shadow, rewrite, deletion, and unknown-path drift remains unsafe.
- `foundation_safety.free_source_health`: aggregate collection status, session counts, provider
  health-label counts, public problem source IDs, lane statuses, and operator actions. It contains
  no provider values or raw payloads and grants no source/Buy/execution authority.
- `foundation_safety.free_source_alert`: validated alert severity, active condition codes,
  transition, and local-notification decision. It always reports
  `notification_delivery_confirmed=false` because the evaluator decides but does not send; it is
  not a production gate.
- `foundation_safety.candidate_quote_coverage`: validated aggregate state and fresh/actionable/
  unavailable counts for the post-discovery candidate quote bundle. It contains no prices or
  account data. Ordinary missing/unavailable coverage degrades research; invalid artifacts or
  health receipts are `at_issue`. It never grants market-anchor, Buy, or execution authority. See
  [`docs/candidate_quote_hydration.md`](candidate_quote_hydration.md).

`foundation_safety.production_gate_enforced=false` during this canary phase. Missing evidence is
`unverified`, never zero. Free-source health is an independent diagnostic projection and does not
change the foundation status or become a production gate. An observed preview/write or unknown
broker attempt is nevertheless denied immediately by the hook and forces the Claude wrapper to
fail.

The 2026-07-10 canary is therefore represented without contradiction as
`scheduler=healthy`, `morning_workflow=failed_safe`, `morning_slo=missed`, and
`overall_status=at_issue`. The reconciler is diagnostic-only, self-hashed, contains no account
data, and cannot schedule a run, classify a candidate, alter a report, or grant authority.
The local alert evaluator mirrors these research-artifact reason codes so a green scheduler cannot
hide an invalid candidate bundle or a false post-open skip. This projection remains diagnostic
only and cannot repair history, rerun a workflow, or change any trading conclusion.

Machine wake readiness is deliberately separate from LaunchAgent configuration. A loaded,
byte-matching intake agent can be `scheduler_config_health=healthy` while a 05:43 repeating wake
produces `power_schedule_health=at_issue` because intake starts at 05:00. The intake runner warns
but does not reject an already-awake catch-up run. The installer inspects and prints the exact
administrator remediation without executing it. See
[`docs/power_schedule_health.md`](power_schedule_health.md).

Service timeliness is also independent. Every supervisor terminal path performs one bounded,
secret-stripped, read-only refresh of
`data/runtime/health/<target>_autonomy_service_health.json`. A healthy or `at_issue` receipt is a
successful refresh; structural refresh failure is returned as diagnostic metadata but cannot
change the runner's state or exit code. This lets a later session expose an earlier missed start
without spending model tokens. Missing-run continuity does not relabel a late start as timely. See
[`docs/autonomy_service_slos.md`](autonomy_service_slos.md).

Self-refresh cannot detect a job that never starts. The independent watchdog therefore runs at
05:21, 05:46, 06:16, 07:06, and 14:16 PT, exactly one minute after the five expected-terminal
deadlines.
At 05:46 only, it first requests the fixed zero-attempt macro continuity path; all other checkpoint
invocations make that request a write-free no-op. It then regenerates composite health, emits one
fixed local alert for a new or changed missed/invalid SLO or scheduler-contract issue, suppresses
unchanged incidents, and emits at most one alert recovery transition. Pending objectives and the
separate machine-power issue do not trigger this alert. The alert evaluator never sends externally,
invokes a model/broker, retries a workflow, or changes a schedule. See
[`docs/autonomy_service_watchdog.md`](autonomy_service_watchdog.md).

The Morning DAG is a separate shadow experiment, not a fourth health plane and not production
authority. Its runner hook is plan-only, launchd does not opt in, and attended worker execution
requires a second explicit CLI flag. Shadow terminal states never satisfy the supervisor's
`morning_terminal_singleton` postcondition.

## Commands

Validate the shipped contract, LaunchAgent definitions, and broker deny list:

```bash
python3 scripts/autonomy_orchestrator.py check
```

Inspect today's state without creating it:

```bash
python3 scripts/autonomy_orchestrator.py status
```

Exercise the fixed recovery path without network/model/broker access, using a temporary autonomy
state. The standard runner dry-run still records its ordinary local `dry_run_ok` task status:

```bash
python3 scripts/autonomy_orchestrator.py --state-root /tmp/trading-macro-recovery-canary \
  recover-macro --trigger watchdog \
  --target 2026-07-16 --dry-run --now 2026-07-16T05:46:00-07:00
```

Run an attended dry-run canary through the supervisor:

```bash
TRADING_AUTONOMY_STATE_ROOT=/tmp/trading-autonomy-canary \
  python3 scripts/autonomy_orchestrator.py run --workflow morning --dry-run
```

The same supervisor can be exercised through the existing runner delegation path:

```bash
TRADING_AUTONOMY_ENABLE=1 DRY_RUN=1 \
TRADING_AUTONOMY_STATE_ROOT=/tmp/trading-autonomy-canary \
  bash scripts/run_morning_scan.sh
```

Dry-run bypasses the workflow clock only so an attended canary can exercise the control path; it
still validates the market day, integrity manifest, and broker deny list. It succeeds only when the
child writes a fresh `dry_run_ok` task state, and the child runner still exits before model
generation. `--now` injection is rejected unless `--dry-run` is present.

## Activation and Rollback

Repository LaunchAgent plists carry `TRADING_AUTONOMY_ENABLE=1`. The three original session jobs,
the 05:00 intake job, and the independent four-checkpoint watchdog use narrow installs with no
kickstart. Scheduler health treats a missing supervisor opt-in or missing expected label as
a configuration error, so partial upgrades remain visible. Any later plist change still requires
the explicit installer flow before it affects the loaded service.

`scripts/install_launchd.sh --confirm` treats repository plists as integrity-protected templates.
It validates label, schedule, runner suffix, absolute working-directory shape, supervisor opt-in,
and runner executability before any mutation, then atomically materializes the current repository
root and current user's log directory into the installed copy. When the repository already lives
at the canonical production path, the installed plist remains byte-identical to the template.
`--plan` and `--status` remain read-only; a restored snapshot can therefore be validated anywhere
without pretending its temporary path is the active production installation.
When the selected labels include intake, those modes and `--confirm` also run the read-only power
inspection. The installer never invokes `sudo`; changing the machine wake time remains a manual
administrator action.

The watchdog can be inspected or installed without touching the four workflow agents:

```bash
bash scripts/install_launchd.sh --plan --only watchdog
bash scripts/install_launchd.sh --confirm --only watchdog
```

Rollback is narrow:

1. set/remove `TRADING_AUTONOMY_ENABLE` in the repository plist through approved change control;
2. reinstall only the affected label;
3. the runner immediately returns to its historical direct path;
4. retain `data/runtime/autonomy/` as diagnostic history.

No strategy, risk, report, ledger, or broker-permission rollback is involved because the supervisor
does not own those authorities.

## Data Layer

The typed adapter and point-in-time ingestion layer now follows this control-plane contract. See
[`docs/data_lake.md`](data_lake.md). The disabled 30-session provider evaluation is documented in
[`docs/provider_bakeoff.md`](provider_bakeoff.md). Its next gate is written licensing and real
shadow observations, not additional strategy or execution authority.

The deterministic catalyst/event graph is documented in
[`docs/catalyst_graph.md`](catalyst_graph.md). It is a post-freeze diagnostic artifact and is not a
control-plane dependency or trading gate.
