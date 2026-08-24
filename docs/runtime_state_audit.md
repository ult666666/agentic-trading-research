# Runtime-State Audit and Reset-Safety Classification (2026-07-24)

Phase-2 deliverable of the research-qualification milestone. Source: read-only
audit at HEAD `2c3a2e0`. Context: the 2026-07-23 `git reset --hard` incident
(see `reports/incidents/2026-07-23_git_reset_data_loss.md`) proved that
runtime-mutable state stored as git-tracked files is erasable by git history
operations. Commit `2b9870e` applied the incident-§5 recommended core
(untracked + gitignored `data/system_state.json`, `data/run_diagnostics.json`,
`data/research_ledger.lock`). This audit classifies everything else.

## Classification

| Class | Meaning | Reset-safe? |
|---|---|---|
| immutable-config | versioned contracts; change-control only | tracked by design |
| versioned-evidence | historical records whose git history IS the audit value | tracked by design |
| runtime-mutable | machine-written operational state | must be ignored/untracked |
| derived-cache | recomputable | should be ignored |
| secret | credentials | never tracked (gitignored `secrets/`) |

## Correctly reset-safe today (gitignored)

`data/system_state.json`, `data/run_diagnostics.json`,
`data/research_ledger.json` + `.lock`, `data/portfolio.json`, `logs/*.log`,
the entire `data/runtime/` tree (research artifacts, health receipts,
autonomy day-states, broker audit, acceptance rollups, fence staging,
release provenance, Morning engineering observer/iteration receipts and projections) — and the new `data/runtime/shadow_qualification/` root
used by `sim/research_qualification/` (all writers call
`state_safety.assert_reset_safe`, which refuses tracked or unignored paths).

Note: `2b9870e` untracked `data/system_state.json` IN PLACE (Option A). No
redirect exists; `scripts/lib/common.sh record_state()` still writes that
path, which gitignore now suppresses. `data/runtime/system_state.json` does
not exist — earlier session notes claiming a redirect are wrong.

## Still git-tracked AND runtime-mutable (reset-erasable — decision pending)

The §5 remainder, awaiting Charles's explicit call (preview:
`python3 -c "from sim.research_qualification.state_safety import migration_preview; import json; print(json.dumps(migration_preview(), indent=2))"`):

| File | Writer | Notes |
|---|---|---|
| journal/trading_journal.jsonl | scripts/lib/journal_event.py | append-only; git history has real audit value |
| journal/lessons.md | scripts/postmortem.py | prose learning log |
| journal/rejected_setups.md | scripts/rejection_ledger.py | |
| journal/improvement_backlog.json | operator-reviewed `scripts/improvement_review.py` use only | Scheduled Morning/audit writers are prohibited; unattended iteration lives under `data/runtime/`. |
| data/watchlist.json | run_post_close_review.sh (model repair path) | |
| data/shadow_ledger.json | scripts/outcome_tracker.py | |
| data/source_health.json | scripts/source_router.py | |
| data/candidate_funnel.json | scripts/rejection_ledger.py | |
| data/av_budget.json | scripts/av_budget.py | counter |
| data/av_cache/*.json | derived cache | should simply be ignored |
| data/discovery_pool.json, data/current_universe.json, data/catalyst_calendar.json | intake/universe writers | |
| data/replay_ledger.json, data/replay_bars.json | replay session | replay_bars is also an evidence corpus — candidates for manifest pinning instead |

Options per file: **A** untrack+ignore (as 2b9870e; loses future commit-based
tamper evidence, keeps history) or **B** redirect the writer into
`data/runtime/` via an operator pack (keeps the tracked file as frozen
historical evidence). Recommendation mirrors the incident report: A for pure
heartbeat/cache, deliberate per-file decision for the journal trio where git
history is genuine audit evidence. **No file was migrated in this milestone**
— writers are in hook-protected `scripts/`, and the decision is Charles's.

## Write-convention hardening observations (operator-pack candidates)

- `scripts/lib/journal_event.py` appends with no flock and no fsync
  (torn-line risk under the documented concurrent-writer environment);
- `scripts/lib/common.sh record_state` lacks fsync before rename.

## 2026-08-11 Morning hardening addendum

`scripts/run_diagnostics_emit.py` now publishes its ignored diagnostics with file+parent fsync and
atomic replace. It is deliberately pre-finality and never advances an improvement lifecycle. The
post-Morning supervisor is the sole unattended writer of
`data/runtime/morning_engineering_audit/iteration_backlog.json`; each transition is bound to a
validated immutable audit by a create-once intent, projection snapshot, and receipt. The tracked
`journal/improvement_backlog.json` remains operator-reviewed history and is never rewritten by a
scheduled audit.

The exact release snapshot remains the forensic/restore image of the complete worktree and reports
its status honestly as `drift` after legitimate generated outputs. The lock-taking runtime gate now
derives a separate fail-closed source binding: HEAD, branch, package integrity, committed CRITICAL
bytes, staged state, executable shadows, and every unapproved path must remain exact. Only bounded
create-once report paths, strict append-only journal suffixes, and a watchlist rewrite revalidated
through the canonical post-close terminal chain may be classified as expected output drift. No
automatic commit, manifest rebuild, release re-signing, or snapshot creation is permitted.

New qualification/shadow writers do NOT inherit these gaps: they use
`sim/research_qualification/state_safety.py` (create-once fsync+link
receipts, atomic fsync+replace rollups, flock'd hash-chained append-only
ledgers with truncation/regression/duplicate/clock-rollback detection and
pre-mutation recovery snapshots). Reset-safety is proven by test
(`test_git_reset_hard_cannot_erase_runtime_state`).
