# Production settlement procedure

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: adopted 2026-07-27 after the manifest-drift outage of that morning.
Authority: this document describes the required ORDER of operations. The enforcing code is
`scripts/build_manifest.py`, `scripts/release_snapshot.py`, `scripts/runtime_provenance.py`,
`scripts/next_session_readiness.py`, `scripts/worktree_owner.py`, and
`scripts/lib/common.sh` (`.run.lock`).

## 1. Why this exists

On 2026-07-27 every scheduled workflow — intake 05:00, macro-refresh 05:35, morning 05:38,
post-open 06:45 — exited immediately with `integrity_manifest_gate_failed` (exit 78), and the
watchdog refused to evaluate at 05:21, 05:46, 06:16 and 07:06. No research was produced for the
whole trading day.

Nothing was broken in the trading logic. A coding session had ended the previous evening with
three modified CRITICAL files and no manifest re-sign and no commit. The fail-closed gate did
exactly what it should. **The defect was in how the session ENDED, not in what it built.**

The same failure then reproduced *during* the remediation: a human re-signed the manifest at
12:03 PT, and a second concurrent Claude session applied an operator patch at 12:31 PT touching
six CRITICAL files. By 12:38 PT the manifest was drifted again. Without intervention the
2026-07-28 05:00 run would have died at the identical gate.

Two distinct root causes, two distinct guards:

| Root cause | Guard |
|---|---|
| A session ends with reviewed-but-uncommitted control-code changes | unsafe runtime-source drift (historically surfaced as release-provenance drift) + this procedure |
| Two agent sessions mutate one worktree | `scripts/worktree_owner.py` + the session-start hook |

## 2. Exact release forensics and runtime-source authority — MANDATORY RUNTIME GATE

**Historical incident record (2026-07-29 through 2026-07-31):** readiness-only reporting was
insufficient. Uncommitted control code ran while the release snapshot already reported drift. At
that time the system treated every non-`match` snapshot result as `release_snapshot_not_current`.
That history remains valid; the current gate now separates two facts that the original design
collapsed:

1. `scripts/release_snapshot.py verify-current` is the exact forensic comparison. Its `status`
   remains `drift`, and `release_reproducible` remains false, whenever HEAD, index, worktree status,
   patches, or non-ignored untracked inventory differ from the captured snapshot.
2. `scripts/runtime_provenance.py` is the runtime-source authority. It may return a passing
   `runtime_binding_status` of `match` or `expected_output_drift`. The latter is allowed only when
   deterministic validators prove that every difference is a recognized non-executable
   create-once report, strict HEAD-prefix journal append, or receipt-bound output rewrite, while
   committed control bytes, the manifest, HEAD, branch, and index remain safe. This runtime-source
   conclusion grants no report/market authority; report consumers keep their independent finality
   validation.

`expected_output_drift` never rewrites the forensic result to `match` and never makes
`release_reproducible` true. It is a bounded statement that the running control source still binds
to the reviewed release despite separately visible generated evidence. Staged changes, modified or
missing protected/runtime source, report deletion or rewrite, append-prefix violations, unknown
untracked paths, invalid receipts, and every unapproved difference remain fail-closed. Those cases
retain `release_snapshot_not_current`, block readiness, and return 78 at the lock boundary.

Neither status independently grants or alters an Official classification. The gate only decides
whether the settled runtime source may reach research; a missed Morning session stays missed.

Evidence in code:

- `scripts/next_session_readiness.py` lists `release_provenance` in its REQUIRED checks. A safe
  runtime binding passes; receipt-validated `expected_output_drift` remains a visible advisory.
  Unsafe or unapproved drift makes the readiness receipt `status: blocked` with
  `manual_action_required`.
- Every production runner enters through `scripts/lib/common.sh::acquire_lock`. After acquiring
  exclusive ownership and installing the release trap—but before returning to its caller—the
  lock invokes `scripts/runtime_provenance.py`. A binding other than `match` or validated
  `expected_output_drift` returns 78 and the runner stops before provider, model, broker-read,
  research, freeze, ledger, or report work.
- `scripts/autonomy_orchestrator.py` may pass its own pre-child checks before this refusal. That
  does not bypass provenance: the child runner still fails closed at the shared lock boundary.

That composition is deliberate and must be preserved: the manifest binds protected file bytes; the
release snapshot records exact branch/HEAD/patch/status/inventory forensics; runtime provenance
revalidates committed control bytes and admits only its fixed generated-output exceptions. Never
treat forensic drift alone as permission, broaden the output allowlist ad hoc, or bypass the gate to
recover a missed session. Repair unsafe settlement and validate the next session.

**A release snapshot must never be regenerated merely to turn a gate green.** Its whole value is
that it binds a specific committed HEAD to a clean worktree. `release_snapshot.py verify-current`
compares `head_commit`, `head_ref`, `status_v2_z`, `staged_patch`, `unstaged_patch` and
`untracked_inventory`. Expected generated-output drift is retained as forensic evidence and is not
a reason to refresh the snapshot. A settlement snapshot taken over an unreviewed dirty tree proves
nothing about review or runtime-source safety.

**Corollary — a rebuilt manifest does NOT certify a dirty tree.** `build_manifest.py --check`
proves only that file hashes match the manifest *at this instant*. It says nothing about whether
the changes were reviewed, tested, or committed. That is exactly why 2026-07-27 12:03 → 12:31
happened. Manifest PASS is necessary, never sufficient.

## 3. The required order

Never reorder these. Steps 8–10 are the ones that were skipped on 2026-07-26.

1. **One mutation owner.** `python3 scripts/worktree_owner.py check` must not report a live
   foreign owner. Parallel agent work requires a separate `git worktree`.
2. **Safe production window.** If a scheduled workflow could fire during settlement, hold it with
   the repository's own mutual exclusion (`.run.lock`, see §4). Never edit or unload launchd
   plists, and never change the 04:55 weekday wake schedule.
3. **Full diff review.** Read every modified and untracked file, not a sample.
4. **Targeted tests** for the changed subsystems.
5. **Broader acceptance tests.**
6. **Rebuild the manifest** — only after the source state is final.
7. **`build_manifest.py --check` must PASS** with zero drift.
8. **Stage explicit paths.** Never a blanket unreviewed `git add -A`.
9. **Commit**, including the matching `data/MANIFEST.json` in the same commit, so checking out
   that commit is internally consistent.
10. **Clean worktree** — `git status` empty.
11. **Release snapshot** on the canonical branch, bound to the final HEAD.
12. **Next-session readiness** — no blocker, no `manual_action_required`.

## 4. Holding scheduled work during settlement

There is no separate maintenance subsystem, and none should be invented. The repository already
has exactly one: the atomic `mkdir` lock in `scripts/lib/common.sh`.

Every runner calls `acquire_lock` first. A lock directory whose recorded holder PID is alive —
or which is younger than 6h with no PID recorded — makes a scheduled runner log and exit **75**
(skip, no overlap) before it launches Claude, a broker collector, or any report writer.

Verified behaviour (`acquire_lock`, `scripts/lib/common.sh`):

| lock state | result |
|---|---|
| no lock | acquires, rc=0 |
| empty lock, < 6h old | **rc=75, fail-closed skip** |
| lock with a LIVE pid | **rc=75, fail-closed skip** |
| lock with a dead pid | stale, reclaimed, rc=0 |
| empty lock, > 6h old | stale, reclaimed, rc=0 |

The 6h self-expiry is a safety property: an abandoned hold can never wedge the next trading day.
Release the hold as soon as settlement finishes, then re-run readiness — readiness treats a
present `.run.lock` as the blocker `global_run_lock_present`.

## 5. Parallel Claude Code work

A second production mutation session in the same directory must be rejected. Use:

```
git worktree add ../trading-system-<topic> <branch>
```

`scripts/worktree_owner.py` keys its state on the realpath of the worktree, so separate
worktrees never conflict and the same directory always does. Ownership identity is
`(pid, process start time)`; a recycled PID is correctly treated as stale, and a process *name*
is never the test.
