# CODEX BUILD BRIEF — `sentry`: continuous simulation + auto-repair harness for `trading-system`

You are building a continuously-running defect-finding harness inside `/Users/prince/Projects/trading-system`, a Claude × Robinhood **research-only** swing-trade system. Read `CLAUDE.md` first; it is the project constitution and is binding on everything you write.

This brief is self-contained. Every path, line number, and constant below was verified on disk on 2026-07-21. Verify anything load-bearing again yourself before relying on it — the working tree changes underneath sessions.

---

## 1. Mission

The research pipeline works. Every lost trading morning in this system was **plumbing**: seams between components, in time, in concurrency, and in deployment state. There are 154 `scripts/test_*.py` files and **not one of them** could have caught any of the four incidents below. The suite is a record of past fixes, not a search for future ones. Worse, each fix created the next failure.

Your job: build a harness that continuously finds real defects in the seams, classifies them honestly, publishes them where a human actually sees them, and — for a narrow, mechanically-fenced subset — drafts an operator-applied fix that **cannot** be a gate relaxation.

The user's stated pain, verbatim: *"I am tired of mistakes happening over and over again."*

The success metric is **not** more trades. The account is ~$50 at 1% risk per trade. Across ~20 sessions the system sealed exactly two authoritative morning artifacts (`reports/morning/2026-07-16.md`, `reports/morning/official_research/2026-07-13.md`) against 13 preliminaries and 6+ sessions with nothing at all. The target is:

1. Sealed-artifact yield from ~10% of sessions toward ~100%.
2. Every non-sealed session leaves a **labeled, human-visible** artifact.
3. Time-to-human-readable-explanation for a lost morning drops from "reconstruct a 25 MB log by hand" to "read one receipt."

---

## 2. The four incidents — these are your acceptance tests

### INC-1 — 2026-07-17, rc=124, `claude_timeout`
Claude run killed after 1141s. Root cause: the fan-in stage (evidence-adjudicator → final-strategy-synthesizer → freeze) exceeded its budget; `operator_patches/2026-07-17_monday_fix_pack/apply.py` FIX E rebased the adjudication budget 75s → 660s with the note "measured reality 9–12 min." rc=124 set at `scripts/run_morning_scan.sh:913`. Four sessions lost to this class total (07-07, 07-08, 07-10, 07-17). Budgets live in `config/research_budget.yaml` (stages sum to 1920s; `total_deadline_pt 06:10`, `latest_freeze_pt 06:20`, `per_agent_timeout_seconds 480`).

**The catchable signal existed nine days early**: measured stage latency in `data/runtime/health/<date>_timeline.json` crossed the budget on 07-08 and 07-10.

### INC-2 — 2026-07-20, rc=88, `control_plane_integrity_violation`
The autonomy watchdog tick at 05:46 PT called `record_state()`, writing `data/system_state.json` (a CONTROL_FILE, `scripts/control_plane_integrity.py:49`) **inside** the audited morning child's lifetime (05:38–06:07 PT). The envelope guard is authorship-agnostic by design, so it quarantined a **completed, clean run**. `CONTROL_PLANE_VIOLATION_EXIT = 88` at `scripts/audited_claude.py:32`, set at :426. The watchdog takes no lock and ticks at 05:21/05:46/06:16/07:06/14:16.

Root cause chain: `56a79d4` moved morning 05:50→05:32 (creating a lock race with the 05:35 macro job), then `c0226e7` moved it 05:32→05:38 — which pulled the 05:46 watchdog tick inside the audited window for the first time.

### INC-3 — 2026-07-21, rc=90, `model_integrity_binding_invalid` — **caused by the INC-2 fix**
The 07-20 patch added `COMPARISON_EXEMPT_CONTROL_FILES = {"data/system_state.json"}` and excluded it from `changed_paths`/`ok`, but left `before_sha256`/`after_sha256` computed over the **full** projection. The receipt therefore asserted `control_plane_unchanged: true, changed_control_paths: []` while publishing two different digests (`before=4beb8a2db60770a7…`, `after=a1cabec407fe763a…`). `scripts/model_integrity_binding.py` correctly refused the contradiction (`integrity_receipt_control_hash_invalid`); rc=90 set at `scripts/run_morning_scan.sh:967`. A complete, clean `no_trade` run was discarded. The patch author computed it "recurs EVERY morning."

This is the purest instance of the pain: **the 07-20 fix did not remove the false positive, it moved it from the wrapper layer to the re-anchor layer.** The fix is now in: `scripts/control_plane_integrity.py:203-236` (`_compared_projection()` / `comparison_digest()` / `compare_control_plane()` all use the exempt-filtered projection).

### INC-4 — 2026-07-17 and 2026-07-20, exit 78, `integrity_manifest_gate_failed`
`post_close_review` never executed at all on either day. It refused before the runner was invoked because control code changed without a matching `python3 scripts/build_manifest.py` re-sign. `data/runtime/autonomy/2026-07-17.json` and `2026-07-20.json` show `attempt_started` → `attempt_finished | integrity_manifest_gate_failed` with **no `runner_started`**. `reports/post_close/` stops at `2026-07-16.md`. **Nobody noticed for five days.** `journal/system_changes.md:5989-5995` names this "the standing root cause of the chronic `data_readiness_degraded` at intake."

This class fired 5 times total (2026-07-14 intake + morning + post_open; 07-17 post_close; 07-20 post_close) and is **self-inflicted by the repair process itself** — every operator fix touches `scripts/` or `.claude/`, so every fix arms this trap until the ceremony completes.

### The amplifier that made all four invisible
`external_notification_enabled: false` in `config/autonomy_service_alerts.json:37` and `config/free_source_health_alerts.json:17` — and it is a **hardcoded Python constant** at `scripts/autonomy_service_alert.py:45` and `scripts/free_source_health_alert.py:43`, **asserted False** by `scripts/test_free_source_health_alert.py:54`. The suite pins the silence.

Separately, the rc=88/89/90 branch in `scripts/run_morning_scan.sh` terminates at `:1119 exit 1`, **before** the section-9 learning loop at `:1519` and its append-only journal event at `:1525-1527`. Verified: `journal/trading_journal.jsonl` has `morning_run_terminal` rows for 2026-07-09/13/15/16 and **none** for 07-17, 07-20, 07-21. `data/runtime/health/2026-07-21_run_status.json` has `preliminary_checkpoint_written: false`.

`scripts/timeout_checkpoint.py` (340 lines) already implements the correct salvage pattern for rc=124 — a labeled Preliminary under `reports/morning/preliminary/`, never the Official slot, no receipt, no ledger. It is your fix template, not a new mechanism to invent.

---

## 3. Delivery mechanics — read this before writing any file

Both agent hook copies are **byte-identical** (verified sha256: `protect_paths_bash.py` = `d5dd1553…`, `protect_risk_limits.py` = `563ab428…` for both `.claude/hooks/` and `.codex/hooks/`). **You are under exactly the same restrictions Claude is.** Do not assume otherwise.

### What you CANNOT do
- **Write/Edit anywhere under** `scripts/`, `config/`, `schemas/`, `prompts/`, `launchd/`, `.claude/`, `.codex/`, `.git/`, `secrets/` — matched at **any path depth**. `_control_hit` (`.claude/hooks/protect_risk_limits.py:91-103`) matches `norm.startswith(prefix) or ("/" + prefix) in norm`. So `sim/config/x.json` is **BLOCKED**. So is `sim/scripts/`, `sim/prompts/`, `sim/schemas/`, `sim/launchd/`.
- Write any file named `CLAUDE.md` or `AGENTS.md` **anywhere** (CONTROL_SUFFIXES matched by basename).
- Write `data/MANIFEST.json`, `data/research_ledger.json`, `data/shadow_ledger.json`, `data/trial_baseline_manifest.json`, any `freeze_receipt.json`, or anything under `data/runtime/{preflight,robinhood,rh_candidate_quote_raw,broker_audit}/`.
- Run any **mutating** git subcommand (`add`, `commit`, `checkout`, `switch`, `stash`, `reset`, `apply`…). `git status`, `git diff`, `git stash list`, `git rev-parse`, `git branch --show-current` are allowed.
- Run `python3 scripts/build_manifest.py` (re-sign). `--check` and `--help` are allowed.
- Apply patches (`patch`, `apply_patch`) or use `mkdir/cp/mv/tee/touch/rm` on protected paths, or redirect output into them.

The Bash hook also **over-blocks read-only work**: `git reflog`, `wc -l` on an argv containing `scripts/audited_claude.py`, and `awk` reading `scripts/run_morning_scan.sh` were all refused in prior sessions. Design around this: the harness must be invoked without protected paths in its argv, reading its path config from a file.

### What you CAN do
- Write a **new top-level** directory. `sim/` is verified ALLOW.
- Write `sim/scenarios/`, `sim/expected/`, `sim/fixtures/`, `sim/policy/` (**never** `sim/config/`), `sim/README.md`.
- Write `reports/harness/**`, `journal/**`, `data/runtime/**` (except the protected fragments above), repo-root files (`Makefile`, `conftest.py`).
- Write `operator_patches/**` — including a new `apply_fix.py`.
- Run `python3 sim/anything.py` and `bash sim/run.sh`.

### The hole you must close FIRST (Step 0 — ships before any harness code)
`operator_patches/` is in **neither** hook's prefix list. `.claude/settings.json` `permissions.allow` contains `Bash(python3:*)` with `defaultMode: acceptEdits`. All 47 `*.py` files under `operator_patches/` contain **zero** human-confirmation guards — no `input()`, no `/dev/tty`, no `isatty`, no `getpass`. Existing appliers do `p.write_text()` into `scripts/`, re-sign the manifest, and `git add`/`git commit` (`operator_patches/2026-07-21_envelope_digest_binding_fix/apply_fix.py:326-346`).

**A model can therefore author a full control-plane writer and execute it in one turn.** Until this is closed, every safety claim in this brief is decoration. See §5, Stage 0.

### Where harness code lives — three tiers, and this is not negotiable

| Tier | Location | Written by | Why |
|---|---|---|---|
| **A** — decision logic, gates, fingerprints, locks | `scripts/sim/*.py`, `config/gate_semantics.lock.json`, `config/sim_expectations.lock.json`, `launchd/com.charles.trading.sentry.plist.template` | **Operator applier only** | Inside `control_plane_integrity.CONTROL_TREES`, so the audited morning child is OS-denied from tampering with the thing auditing it; and manifest-covered so it cannot drift silently |
| **B** — declarative candidate data | `sim/scenarios/`, `sim/fixtures/`, `sim/expected/`, `sim/policy/` | You, directly | Fast iteration, zero execution authority, zero decision logic |
| **C** — outputs | `data/runtime/harness/cycles/<opaque-id>/`, `reports/harness/`, `journal/harness_findings.jsonl`, `logs/harness.log` | Harness at runtime | Outside CONTROL_TREES and CONTROL_FILES; gitignored where high-volume |

**Stage the tiers.** Keep as much as possible in `sim/` for as long as possible. Every Tier-A change requires editing the hand-written `CRITICAL` list inside `scripts/build_manifest.py` (itself CRITICAL, 597 entries today) and re-signing — which **arms INC-4, the highest-cost failure class**. Your own iteration loop is the dominant generator of the class you are building this to detect. Only the gate-semantics fingerprint, the signed locks, the forbidden-change classifier, and the scenario/suite runners genuinely need Tier A protection.

`build_manifest.check()` (`scripts/build_manifest.py:769-789`) only hashes the hand-written `CRITICAL` list. New files under `scripts/` do **not** break the manifest gate — but they are also **unprotected while appearing protected** until listed. The delivery pack must append every Tier-A path to `CRITICAL` in the same pass, and the harness must refuse to start if any Tier-A file it loaded is absent from `data/MANIFEST.json` `critical_sha256`.

### Applier contract (16 existing packs are the template; imitate `operator_patches/2026-07-21_envelope_digest_binding_fix/apply_fix.py`)
`operator_patches/<YYYY-MM-DD>_<name>/` containing `README.md`, `apply_fix.py`, `backup/` mirror of every touched file, `apply_receipt.json`. Exact-string replacement with before/after anchor verification. **Refuses to write anything if any anchor is ambiguous** — this is also the concurrent-writer guard, and concurrent writers are live (the tree switched branches mid-session on 07-21). Idempotent, all-or-nothing, `--plan` / `--confirm` / `--rollback`. Ends with `python3 scripts/build_manifest.py` re-sign + `--check` PASS + regression tests, aborting **before** commit on any failure. Mandatory `NOT WEAKENED` section enumerating each gate it does not relax, machine-checked (see §6).

---

## 4. Architecture

Name the harness `sentry`. It is **replay-first, hot-loop-cheap, real-process where it must be, and structurally incapable of writing anything that matters.**

### 4.1 Process model

One LaunchAgent, one process, pull-based, no timer writes. Copy the shape of `scripts/operator_console.py` (loopback-only `127.0.0.1:8787`, `serve_forever(poll_interval=0.25)`, 15s cache, **no background writer thread**, KeepAlive for days with zero incidents). Do **not** copy the shape of `scripts/run_autonomy_watchdog.sh` — it takes no lock, calls `record_state()` on every terminal branch, and is the INC-2 mechanism.

Plist shape is pinned:
- `StartInterval` only. `KeepAlive` **absent** — never `true`, never `SuccessfulExit`.
- `ThrottleInterval >= 300`. `ProcessType Background`. `nice`d.
- Dedicated `StandardOutPath`/`StandardErrorPath` with size-based rotation.
- **First action on every start**: verify its own `ProgramArguments` target exists and the repo branch/HEAD matches a pinned expectation. If either fails, write one tripwire and `exit 0` immediately — never loop.

Rationale for the KeepAlive ban: installed plists live in `~/Library/LaunchAgents/`, **outside the repo and outside git**, so a branch switch or `git clean` cannot uninstall them — only delete their targets. `launchd/com.charles.trading.console.plist` uses `KeepAlive=true` + `ThrottleInterval=30`; a harness copying that shape whose entry point is deleted by the branch switcher (the verified 2026-07-21 12:13 PT event removed 5 of 8 entry points from disk) respawns 2,880 times a day forever.

### 4.2 Cadences

| Tier | Cadence | Budget | Content |
|---|---|---|---|
| **T0 hot loop** | `StartInterval` 600s, SAFE windows only | < 20s | Deployment probe, manifest-vs-tree temporal differ, contract/invariant evaluation over receipts written since last tick, absence detection, liveness receipt |
| **T0+** | Triggered by **any detected control-plane change**, within one tick | < 5 min | Full concurrency/envelope scenario set in scratch roots |
| **T1 suite** | 08:15 / 15:15 PT weekdays + 21:15 daily, SAFE only | hard 30 min | The 154 `scripts/test_*.py`, sandboxed, per-file receipt |
| **T2 pre-window verdict** | 04:40 PT weekdays (before continuity at 04:56) | < 60s | Go/no-go readiness statement. The **only** output permitted to be loud in the morning |
| **T3 proposal** | Never on a timer. Manual/queued only | — | Fix-pack drafting |

**T0+ is load-bearing and is the fix for the single biggest weakness of a batch-only design.** INC-3 was caused by a patch applied the evening before. A harness that only runs scenarios at 08:30 the next morning discovers rc=90 two and a half hours *after* the run it would have saved. Triggering on control-plane change means an operator applying a patch at 20:00 PT gets a red scenario at 20:10 PT — nine hours of lead time.

### 4.3 BUSY fence — live-child detection, not the clock, and both

Re-check **immediately before every write**, not once per cycle.

Three pull-based signals, zero contention:
1. Any workflow with `state == "running"` in `data/runtime/autonomy/<today>.json`.
2. `.run.lock/` exists with a live pid.
3. Any `data/runtime/broker_audit/<today>/<run-id>/` lacking a terminal receipt.

**Plus** an absolute wall-clock prohibition on any harness process **existing** (not merely writing) during **04:50–07:15 PT** and **13:10–13:45 PT** on weekdays.

Both are required. Clock-only is wrong because measured morning runs reach 29.7 min (three most recent real runs: 22.0, 29.7, 27.0). Live-child-only is wrong because the snapshot pass precedes `runner_started`, and a lane refused at exit 78 never writes `runner_started` at all.

**BUSY must fail closed AND fail loud.** If any signal is unreadable, stale, or self-contradictory (`state=running` with no live pid; `.run.lock` older than the longest measured lane; a `broker_audit` run dir with no terminal receipt older than 2h), the harness does nothing **and emits `busy_signal_indeterminate` as a finding**. Consecutive skipped cycles are escalated after k. "I never ran" must never look like "nothing is wrong."

### 4.4 Locks

**Never acquire, create, or remove `.run.lock`.** Only `stat` it. Two independent reasons:
- `scripts/run_morning_scan.sh:137` sets `RUN_LOCK_WAIT_SECONDS=840`, so holding it does not fail fast — it silently burns up to 14 minutes of a window with ~12 minutes of slack, then forces exit 75.
- `scripts/next_session_readiness.py:402` gates `runtime_lock_clear` on `.run.lock` not existing, so a leaked harness lock blocks next-session readiness outright.

Self-exclusion: `fcntl.flock` on one fixed file inside the harness output root.

### 4.5 Do NOT source `scripts/lib/common.sh`

It would give you three landmines for free:
- `record_state()` (`:279-309`) is a read-modify-write on `data/system_state.json` — simultaneously a CONTROL_FILE (`control_plane_integrity.py:49`), an authority path (`:260`), and **COMPARISON_EXEMPT** (`:56`), so a lost update there is invisible to the envelope by design. This is the verified 2026-07-21 near-miss where the 13:15 post_close `record_state()` would have erased that morning's `morning_scan = model_integrity_binding_invalid` record; it survived only because a human restored the file verbatim from a stash.
- `notify()` (`:226-231`) is an osascript banner — spam here destroys the only human channel that exists.
- `log()`/`err()` `tee -a` into `logs/automation.log`, currently **25,144,897 bytes with no rotation anywhere in the repo**. Root-causing rc=90 already required reconstructing a transcript from that file by hand.

Carry your own logger (dedicated file, size-based rotation, hard byte cap) and your own timeout helper.

### 4.6 Output path shape — this is a correctness constraint, not a style preference

**NO harness artifact path may contain a session date, a target-date substring, or any `YYYY-MM-DD` token anywhere in its path relative to `data/runtime/`.** Dates go **inside the file body only**.

`_authority_paths()` (`scripts/control_plane_integrity.py:271-291`) recursively walks the entire `data/runtime/` tree and admits **any** path whose lowercased relative posix path *contains* the target session date, excluding only `broker_audit/`, `control_plane_quarantine/`, and `*_autonomy_service_health.json`. So `data/runtime/harness/2026-07-22/findings.json` enters the audited morning child's authority surface. A harness tick during the child's lifetime then makes `compare_authority_surface` report a changed path → `unauthorized_authority_paths` non-empty on a completed clean run; and on a failed child, `quarantine_authority_changes` (`:441-465`) **moves the harness's own file away** and restores pre-child bytes (prior kind `missing` ⇒ it just disappears).

Required layout: `data/runtime/harness/cycles/<opaque-cycle-id>/…` where the cycle id is a non-date-derived hash.

**Belt and braces**: the Step-0 pack must also add `"harness"` to the excluded first-part set at `control_plane_integrity.py:280-282` alongside `broker_audit` and `control_plane_quarantine`, plus a test asserting that for every target date in a synthetic range, no path under `data/runtime/harness/` appears in `_authority_paths(root, target)`. Do not rely on naming discipline alone.

### 4.7 Sandbox — allow-list, not the production deny-list

Every harness process (hot loop, suite runner, scenario runner, report writer) launches under `/usr/bin/sandbox-exec` with a profile that **denies `file-write*` globally** and then allows exactly: the harness output root, its own log file, `reports/harness/`, `journal/`, and (scenario runs only) the scratch root under `$TMPDIR`. Network denied outright. Read of `secrets/` denied outright.

**Do not reuse `_control_write_guard_profile` (`scripts/audited_claude.py:176-204`).** It begins `(version 1)` `(allow default)` and then adds denies for one root's CONTROL_TREES/CONTROL_FILES. It permits writing to literally every other path on the machine. A harness confined by that profile is not confined at all, and hard constraint 2 would rest on code convention — which the watchdog already proved insufficient.

### 4.8 Scratch roots — outside the repository, always

Neutral/scratch trees live under `$TMPDIR` via `tempfile.mkdtemp()`. **Never** under `data/runtime/`, never under `scripts/`, never anywhere inside the repo.

A scratch tree inside the repo enters three sweeps simultaneously: the control-plane rglob (`control_plane_integrity.py:133-147`), the authority rglob (`:271-291`), and the untracked inventory (`release_snapshot.py:173-189`, `--untracked-files=all`). Measured: the control surface is **3,988 files / ~68 MB** (`scripts/` 23M, `.claude/` 44M). `capture_control_plane` reads every one with `keep_bytes=True`, **twice per audited child** plus once more inside `restore_control_plane`'s re-verify. A single in-repo scratch copy inflates all of that linearly on every production morning run, in a window with ~12 minutes of slack and a documented rc=124 history.

Reap on exit **and on startup** (crash-safe orphan sweep). Hard cap on concurrent roots.

### 4.9 Real process vs. pure transform — the dividing line

**Pure `recording -> recording'` transforms** (arithmetic over JSON, seconds, hot-loop-safe) for: receipt perturbation, stage-latency arithmetic against `config/research_budget.yaml`, CLI-output fuzzing of `scripts/classify_claude_failure.py` `_PATTERNS`, late-lane firing, exit-code table checks, schedule-lattice overlap, deployment-surface probing, contract evaluation over real receipts.

**Real OS processes against a real scratch filesystem — MANDATORY, no pure-transform variant permitted** for anything touching: `scripts/control_plane_integrity.py`, `scripts/model_integrity_binding.py`, `scripts/audited_claude.py`, `scripts/research_freeze.py`, `scripts/freeze_report.py`, and `scripts/lib/common.sh` `acquire_lock` / `record_state`.

Rationale, carry it verbatim into your code comments: **`compare_control_plane(before)` re-captures the after-state from `before['root']` on the real filesystem (`control_plane_integrity.py:221-223`).** A transform that hands the comparator a synthesized "after" validates only the harness's own Python model of the code — and that model, written *after* the 07-21 fix, already encodes the corrected behavior, so it would have **missed rc=90 entirely**. `scripts/test_control_plane_integrity.py:304-313` does the real-root setup in six lines of `tempfile.mkdtemp`; there is no excuse.

**The harness must not contain a Python re-implementation of any bash function it tests.** The lock and `record_state` faults invoke `scripts/lib/common.sh` via a real shell in a real scratch tree with real concurrent OS processes. A transcription drifts silently from the bash it mirrors and produces a false green (real bug invisible) or a false red (finding that does not exist in production), with no oracle to say which.

### 4.10 Invocation discipline for the scratch root

`scripts/audited_claude.py:239-245` sets `canonical_root = (root == ROOT.resolve())` and defaults `enforce_os_write_guard` to `canonical_root`.

⇒ If you invoke the **production** `audited_claude.py` with `--root` pointing at scratch, `canonical_root` is False and the child runs with **no Seatbelt profile at all** — an unsandboxed child spawned by the safety harness.

⇒ Therefore: **always invoke the COPY inside the scratch tree** (so `ROOT` derives from the copied `__file__` and is canonical to the scratch), and **assert `enforce_os_write_guard is True`** rather than relying on the default.

### 4.11 Never monkeypatch `ROOT` / `CODE_ROOT`

`scripts/research_freeze.py:1296-1300` raises `canonical_freeze_test_injection_forbidden` when the selected reports root equals `(CODE_ROOT/'reports').resolve()`. Monkeypatching `CODE_ROOT` to a scratch path makes that comparison `scratch == scratch`, which **disables the guard rather than satisfying it** — and puts a 10-minute-cadence process one bug away from writing `reports/morning/official_research/<date>.md`. Drive those modules only as subprocesses from inside the copied tree, with an explicit non-canonical `reports_root`.

### 4.12 Reuse map — extend, do not rebuild

The repo already has a large simulation estate. Duplicating any of it is the design failure.

| Need | Reuse |
|---|---|
| Process supervision, absolute deadlines, cancellation, immutable terminal persistence, `SAFETY` block convention | `scripts/morning_dag.py` (1055 lines; `SAFETY` at `:62-76`: `research_only`, `shadow_only`, `execution_enabled: False`, `feeds_production: False`, `grants_buy_authority: False`, `broker_access: "none"`) |
| Check vocabulary | the 17 `CHECK_IDS` at `scripts/natural_run_acceptance.py:39-45` |
| Scenario-table-with-expectations shape | `scripts/premarket_simulation.py:10-24` (14 scenarios, confined output roots) |
| No-production-side-effect postcondition + PII scanner | `scripts/proof_free_ensemble.py:2-7`, `scripts/proof_level1_prereqs.py:2-5`, `scripts/proof_primary_stale_secondary_live.py:5-19` |
| Injected-clock idiom | `scripts/rehearse_session.py:61-66` (`real_now=` into `freeze_gate.evaluate`, which takes it explicitly at `freeze_gate.py:44,53`) |
| Late-stage salvage artifact shape | `scripts/timeout_checkpoint.py` |
| Fail-closed invariant sets | the four ONE-SHOT selftest shells (`integration_selftest.sh`, `preflight_selftest.sh`, `automation_selftest.sh`, `rh_integration_selftest.sh`) |
| Semantic (not byte) fingerprint pattern | `scripts/broker_runtime_audit.py:235-243` `policy_sha256()`, enforced at `:343-344`, mirrored at `asset_allocation_context.py:524,575-576` |
| Tighten-only overlay + rejection ledger | `scripts/lib/risk_gate.py:36-37` (`_TIGHTER_IS_LARGER`), `:100-128` (`effective_limits`, `ignored_loosening`) |
| Hash-chained append-only events | `scripts/broker_runtime_audit.py:313` (`previous_event_sha256`) |
| Rendered-plist convention | `launchd/com.charles.trading.automationtest.plist.template` (versioned `.template`, rendered plist gitignored) |

**Free integration point, take it**: `scripts/run_morning_scan.sh:762` gates the `morning_dag` shadow canary behind `TRADING_MORNING_DAG_CANARY`, with the comment at `:759` "The LaunchAgent does NOT set this variable." The shadow tree contains exactly one run, from 2026-07-16. Enabling it and diffing the shadow terminal against the production terminal is near-zero cost at an already-integrated seam.

---

## 5. Staged deliverables

Each stage has a crisp definition of done. **Stage 1 must find a real defect on day one.**

### STAGE 0 — Applier authority gate + authority-surface exclusion + notification reachability
**One operator pack.** Nothing else ships before it.

1. Add `OPERATOR_APPLIER_INVOKE_RE` to **both** byte-identical hook copies so `operator_patches/**/*.py` invocation is blocked from model Bash exactly as `scripts/audited_claude.py` already is (`protect_paths_bash.py:77-80`). **MANDATORY: carve out a provably non-mutating `--plan` mode.** Without it you lose the ability to dry-run your own packs and will ship untested appliers to a human — strictly worse than today.
2. Add `operator_patches/_lib/confirm.py` reading a typed confirmation phrase from `/dev/tty`, **failing closed with no controlling terminal**. Every applier calls it as its first statement. All 47 current files have no guard.
3. Add `"harness"` to the authority-surface exclusion set at `control_plane_integrity.py:280-282`, with the synthetic-date-range test described in §4.6.
4. **Abolish the blanket re-sign.** `python3 scripts/build_manifest.py` with no flag (`:800-847`) unconditionally re-hashes whatever is on disk with no declared-change set and no diff. That is the master laundering primitive and it is the terminal step of every applier. Replace with a mode that takes an **expected-change set** (path → new sha256), computes the actual drift set, **refuses if it is not exactly the declared set**, prints the full diff, and requires the same `/dev/tty` confirmation. No path a pack did not declare may ever be signed.
5. **Ship the notification pack here, not in Stage 3.** `external_notification_enabled` is a hardcoded constant in two scripts and asserted False by `scripts/test_free_source_health_alert.py:54` — so it is a code + test + manifest-re-sign ceremony, not a flag. Budget it explicitly. Every one of the four incidents was **detected correctly and reported to nobody**; `journal/improvement_backlog.json` has 22 open items untouched since 2026-06-23. A harness that ships detection ahead of reachability reproduces the exact pathology. Keep the change minimal and scoped to **pipeline-integrity** signals — see §7.
6. Add `reports/harness/` and any other non-gitignored harness output path to `.gitignore`, **or**
   commit them each session. **Historical baseline when this brief was authored:** `.gitignore`
   covered `data/runtime/`, `reports/tests/`, `reports/automation_test/`, `logs/*.log` — but **not**
   `reports/harness/`, **not** `journal/harness_findings.jsonl`, **not** a new top-level `sim/`,
   `operator_patches/`; `next_session_readiness.py` then blocked directly on
   `release_status == 'match'`, and `release_provenance` was the sole blocker on 3 of the preceding
   5 readiness receipts. The exact `release_snapshot` comparison still inventories every
   non-ignored untracked file and retains `status=drift` for any difference. Current runtime binding
   may tolerate only specifically recognized, receipt-validated generated outputs; harness paths
   are not implicitly trusted by that rule. An ignored runtime path or an explicitly designed
   receipt/validator is therefore still required before unattended harness publication. The
   snapshot hard ceilings also remain — 64 MB/file and 256 MB total — and violations raise
   `SnapshotError`, not a soft warning.

**Done when**: applier invocation is blocked from model Bash except `--plan`; every applier tty-gates; the harness-exclusion test passes; declared-change re-signing works and refuses undeclared drift; one pipeline-integrity signal reaches the user on a channel they see; `python3 scripts/build_manifest.py --check` returns PASS.

### STAGE 1 — Read-only observer, entirely under `sim/`, zero operator packs
Ship this **without** an operator pack. You can author and execute everything under `sim/` today (`sim/sentry.py`, `sim/policy/*.json`, `sim/expected/*.json` all Write-ALLOW; `python3 sim/sentry.py` Bash-ALLOW). A read-only observer needs no protection to be useful — it needs protection once it is *trusted*.

Capabilities:
1. **Contract/invariant evaluation over receipts already on disk** — 8 dates under `data/runtime/broker_audit/`, 88 files under `data/runtime/health/`, 10 days of `data/runtime/autonomy/`, 7 dates of `data/runtime/research/`, `data/runtime/readiness/`.
2. **Manifest-vs-tree TEMPORAL differ** evaluated against the **next scheduled lane time** — "manifest has been drifted for 47 minutes and post_close fires in 12." The existing `.git/hooks/pre-commit` validates the *staged index* at commit time, runs exactly one test file, degrades open in three places, and is bypassable with `--no-verify`. Production runs the **working tree**.
3. **Deployment-surface probe**: all 8 installed LaunchAgents (`console`, `continuity`, `intake`, `macro-refresh`, `morning`, `postclose`, `premarket`, `watchdog` — none is a test); every `ProgramArguments` path exists on disk; each resolves to a chain containing `scripts/audited_claude.py` rather than a bare CLI; branch/HEAD vs. declared release pin; unreconciled stashes (two are present right now, both labeled "epitaxy: pre-switch from feature/operator-console-v2"); every executed script path resolves to a committed git object.
4. **Schedule-lattice overlap solver** run against the **currently installed** plists every cycle: continuity 04:56, intake 05:00, watchdog 05:21/05:46/06:16/07:06/14:16, macro 05:35, morning 05:38 + measured p95, premarket 06:45, postclose 13:15. Each lane modeled as `[start, start + p95]` with a declared write-set (does it call `record_state()`? does it take `.run.lock`?), watchdog ticks as instants.
5. **Absence detection** from a **declared registry** (§6, O7), plus a liveness receipt every tick.
6. **Observer-downstream-of-guard static check** — `scripts/run_autonomy_watchdog.sh:117-121` has a default branch `*) err …; notify …; record_state … failed service_alert_evaluator_failed; exit 1;;` that returns **before** the acceptance block at `:131`. Report this pattern as its own finding class.
7. **Findings publication** to `reports/harness/`, `journal/harness_findings.jsonl`, `journal/improvement_backlog.json` (existing `schema_version 1.0` item shape: `id/category/diagnostic/evidence/severity/proposed_action/test_required/approval_required/status/commit_resolved/validation_result/safe_to_implement_now`).

**Done when** all of the following are reported on real current data, unprompted:
- The 2026-07-21 self-contradictory integrity receipt at `data/runtime/broker_audit/2026-07-21/morning-20260721T123801Z-f27286017a18/morning-main-20260721T124124082840Z-70445-5dad9569.integrity.json` (`unchanged=true`, `changed=[]`, `before=4beb8a2d…`, `after=a1cabec4…`).
- The 05:46 and 06:16 watchdog ticks landing inside `[05:38, 05:38+p95]`.
- `post_close` has produced no sealed artifact since 2026-07-16.
- `data/runtime/natural_run_acceptance/` stops at 2026-07-16 and `current.json` is frozen at `2026-07-16T21:16:12Z status=blocked`.
- `data/runtime/health/candidate_artifact_health_rollup.json` at `consecutive_non_healthy_count: 4`, `latest_health_label: "missing"`, `ok: false`.
- **Historical baseline to replay:** `release_provenance` blocked on 3 of the 5
  `next_session_*.json` receipts examined when this brief was written.
- `data/runtime/research/2026-07-21/evidence.json` `conflicts: []` while `agents/skeptic-agent.json` carries `severity='fatal'` twice plus `fatal_circular_sourcing` and `fatal_for_execution` (see O4).
- The observer-downstream-of-guard pattern in `run_autonomy_watchdog.sh`.

### STAGE 2 — Scratch roots + real-process concurrency/timing scenarios
Operator pack promotes the engine to Tier A (`scripts/sim/`) and delivers the signed locks. Ship `scripts/sim/{scratch_root,scenario_runner,chaos,contracts,invariants,lattice,verdict,suite}.py`, `scripts/run_sentry.sh`, `launchd/com.charles.trading.sentry.plist.template`, `config/sim_expectations.lock.json`, plus the `CRITICAL` appends and re-sign.

Also here: **fixture promotion**. The best corpus for this defect class — `data/runtime/broker_audit/` (2026-07-12..21, all three failures), 88 health receipts, 7 dates of real 8-agent evidence — is entirely under the gitignored `data/runtime/` prefix and is one `git clean` or branch switch from deletion. Promote PII-scrubbed copies into a tracked fixture area using the `proof_*.py` scanner discipline (emails, 8+ digit runs, tokens, account keys). **This must happen in week one, not later.**

Scenario set (all real-process where §4.9 requires it): `sibling_control_write` (exempt and non-exempt variants), `exempt_write_digest_probe` (full cross product of {exempt changed, non-exempt changed, both, neither}), `lock_contention` (live pid / dead pid / empty-pid dir aged 7h — the decision table at `scripts/lib/common.sh:168-215`), `interleaved_record_state`, `detached_descendant` (**characterize only**, never auto-repair — a broad PID-kill "fix" would be worse than the hole; `journal/improvement_backlog.json B:2c4fd9a2e8a1`, severity high, open), `kill_9_at_phase` (six boundaries: post-snapshot/pre-child, mid-child, post-child/pre-compare, post-compare/pre-receipt-write, post-receipt/pre-freeze, post-freeze/pre-journal), `stage_latency`, `clock_skew` (including `MAX_STAMP_SKEW_MINUTES=15` straddle at 14 vs 16 min, `freeze_gate.py:41,73-75`; 06:19:59 vs 06:20:01; DST boundary; target-date/PT-date mismatch), `stale_manifest` temporal gap, `config_mutation_between_lanes`, `late_lane` (+1h/+3h/+4h), `truncated_json`/`byte_flip` at every 10% offset over `evidence.json`, `candidates.json`, `freeze_request.json` and the 8 agent artifacts, `cli_output_fuzz` (all 15 `_PATTERNS` + exit 127 + exit 1 unmatched), `provider_pathology` (200-with-stale-timestamp, 402, 429, partial basket — classification from **timestamps**, not status codes), `permission_parity` (static cross-check of every command literal in `prompts/*.md` against the 8 `.claude/settings.json` allow entries), `agent_fabrication` (the 2026-07-15 corpus), `contradictory_fanout`, `post_freeze_emission`, `branch_switch_under_daemon`, `disk_full`.

**Scoped destructive injectors — refusal preconditions, not conventions:**
- SIGKILL targets **only** pids in the scenario runner's own process group, addressed by **pgid**. Never by name, never by pattern, never `pgrep`/`pkill`/`killall`. Verified: no `pkill`, `killall`, or unscoped `kill -<sig>` exists anywhere in `scripts/` today. Do not be the first.
- Every git injector sets `GIT_DIR` **and** `GIT_WORK_TREE` explicitly to the scratch repo and **refuses to execute** if the resolved work tree equals the production root. An unscoped git injector reproduces the 2026-07-21 12:13 PT branch-switch incident verbatim.
- Any injector that deletes or moves a file asserts its target resolves inside the scratch root first.

Also ship the **exit-code semantics contract table** here (§6, O3), and the **T1 suite runner** (§6, O9).

**Done when**: the four-incident replay (§8) passes; `sibling_control_write` reproduces rc=88 for a non-exempt path and *not* for the exempt one, both against real modules in a real scratch root; the lattice check refuses a synthetic plist change that places a `record_state()` caller inside an audited window.

### STAGE 3 — Gate-semantics lock, monotonicity, and the fix-proposal loop
`scripts/sim/gate_semantics.py`, `config/gate_semantics.lock.json`, the mutation self-test corpus, the forbidden-change classifier, the three-key clamp, gate monotonicity property tests, `scripts/sim/propose.py`, and a read-only `/sentry` panel in the existing loopback `scripts/operator_console.py`.

**The proposal loop ships last, deliberately.** A repair loop without the clamp is the single most dangerous artifact this project could produce.

**Done when**: the clamp catches the **real historical weakening** — seed a disposable copy with the actual 2026-07-20 exemption diff (the one that produced rc=90) and require the harness to refuse it, as a precondition to publishing any green verdict. A clamp that has never been observed to fire is not evidence.

---

## 6. Oracles and invariants

Contracts are `(id, producer, consumer, artifact, predicates, severity)`. Evaluator lives in Tier A; **declarations for anything ever red live in the operator-signed lock**, never in the model-writable tier (§7).

### O1 — Receipt self-consistency
Cross-field implications inside one artifact. This is the concept the system lacked entirely; INC-3 was not a missing test, it was a missing idea.

For `control_plane_integrity_receipt/1.0` (verified keys: `schema, target_session_date, workflow, run_id, invocation_id, generated_utc, control_plane_unchanged, before_control_plane_sha256, after_control_plane_sha256, changed_control_paths, control_plane_restoration, broker_violation_detected, authority_output_quarantine, unauthorized_authority_paths, execution_enabled, activation_authority, receipt_sha256`):
- `control_plane_unchanged == true` ⟹ `before == after`
- `changed_control_paths == []` ⟺ `control_plane_unchanged == true`
- `receipt_sha256` recomputes over the other INTEGRITY_KEYS
- `execution_enabled == false` **and** `activation_authority == false`, unconditionally, in every receipt of every schema

Analogues: `frozen == true` ⟹ receipt path exists and hashes match; `report_written == true` ⟹ the named file exists and hashes to the receipt's sha; `status == "failed"` ⟹ `preliminary_checkpoint_written == true` **or** a journal terminal row exists for the target (fails today on `data/runtime/health/2026-07-21_run_status.json`); `counts_in_metrics == true` ⟹ classification ∈ `OFFICIAL_*` and the freeze receipt passed; `ok == false` ⟹ `changed_paths` non-empty.

**Critical limitation you must state in the code and in the report**: O1 **cannot** detect exemption widening. Since the 07-21 fix, `_comparison_projection()` (`:202-219`) filters both the verdict and the digests over the same projection, so an added exemption is internally *consistent*. Only exact membership + cardinality assertion catches it (O10).

### O2 — Producer/consumer field-domain equality
The general statement of INC-3: **the domain over which a producer computes a published value must equal the domain over which the consumer interprets it.** Checked as a property over the **pair**, never as a unit test of either side. Pairs at minimum: `compare_control_plane → model_integrity_binding`, `freeze_request → research_freeze`, `deterministic_adjudicator → research_gate`, `coverage_policy → decision_path`, `broker_runtime_audit → model_integrity_binding`.

Sweep the exempt/non-exempt mutation matrix through `compare_control_plane` and assert `model_integrity_binding` accepts **iff** the producer says ok.

### O3 — Exit-code semantics table
Declarative, in the signed expectations lock, for rc ∈ {0,1,75,78,86,87,88,89,90,124} of every runner: required `FAIL_CLASS` string; whether a terminal artifact must exist; whether a journal row must exist; whether authority surfaces must be byte-identical; whether the run may count in metrics.

**Every required artifact must be bound to that terminal by `run_id`/`invocation_id`** — never satisfied by mere existence, file recency, or an artifact from another branch. Simultaneously assert **exactly one** terminal artifact per session, duplicates quarantined not deleted. Otherwise the visibility fix can be "certified" by reproducing the 2026-07-08 duplicate-terminal defect (`reports/morning/preliminary/2026-07-08_preliminary_live_price_research_incomplete_0615PT.md` **and** `2026-07-08_timeout_checkpoint_preliminary_0616PT.md` both on disk; `journal/improvement_backlog.json C:185759dde62b` open).

Today rc=88/89/90 provably violate the artifact and journal clauses. That is a red test, not a wish.

### O4 — Cross-representation consistency (ranked **above** conservation)
Mandatory: for every agent artifact under `data/runtime/research/<target>/agents/`, if **any** field anywhere in the document carries a severity matching `/fatal|material|blocking/` or a disconfirming/conflict/circular-sourcing body, then the structured channel the deterministic path actually reads — `normalized_claims[].conflicts_with_keys`, and the resulting `evidence.json` `conflicts` — **must be non-empty**, and the fatal rating must survive into `evidence.json`.

Ship this **red on 2026-07-21** as the acceptance test: `agents/skeptic-agent.json` contains `severity='fatal'` twice plus `fatal_circular_sourcing` and `fatal_for_execution`, all inside prose fields (`challenges[]`, `circular_sourcing_found[]`, `liquidity_and_execution_risk[]`); every `normalized_claims[].conflicts_with_keys` is `[]`; `evidence.json` has `conflicts: []` with 0 of 37 claims declaring a conflict.

Note why a naive "conservation" oracle (`agent-declared conflicts == conflicts preserved`) returns **green** here: it compares 0 to 0. And `symmetrize_conflicts()` (`scripts/deterministic_adjudicator.py:613-631`) only resolves `claim['_conflict_keys']`, populated at `:459` from `core['conflicts_with_keys']`.

**Additionally**: assert every severity vocabulary an agent emits is **read by at least one module under `scripts/`**. Today `grep 'fatal'` across `scripts/evidence_schema.py`, `scripts/research_gate.py`, `scripts/deterministic_adjudicator.py` returns **zero hits**, and no script reads `challenges` or `circular_sourcing_found`. The skeptic's veto channel — which `CLAUDE.md` §4 makes load-bearing — is decorative today. That is a standing finding.

Also carry the D1 predicate: **a self-disclosed access failure adjacent to a high-confidence conclusion** blocks promotion. Golden corpus is the 2026-07-15 artifacts (404 FDA URL, 403 EDGAR with the agent's own disclosed 403 next to `high` confidence and a claimed "workaround applied", JPM accession …048086 vs the …048078 a domain search surfaced, figure-free BAC newsroom page). `journal/improvement_backlog.json A:c34e8caa97bd`, severity high, open.

### O5 — Schedule-lattice non-overlap
Model every lane and every timer tick; declare each one's write-set. Properties: no instant-writer whose write-set intersects `CONTROL_FILES` may fall inside any audited child interval; no two `.run.lock` takers may overlap beyond `RUN_LOCK_WAIT_SECONDS`.

Evaluated on the **currently installed plists every cycle**, and as a **hard veto** on any proposed change to a `launchd/*.plist`, `config/research_budget.yaml`, `config/morning_dag.json`, or a lock parameter. Four of the five repair-chain links were timing/ordering changes; this single rule blocks the 05:50→05:32 lock race and the 05:32→05:38 watchdog-inside-window move **before they ship**.

### O6 — Chronic red
Any gate/rollup non-green for ≥3 consecutive sessions with **no remediation event** is its own finding, escalating. A chronically-red gate has stopped being information — that dynamic is what let the 5-day post_close outage run. **Apply this to the harness's own findings too.**

### O7 — Absence, from a declared registry — never learned
**Do not induce "expected artifacts" from observation.** Anything already dead at install time would be baselined as legitimately absent, and anything that never appears would never be expected. Ship a versioned registry enumerating, per lane and per session type, which artifacts must exist and their maximum staleness — **seeded with the known-dead ones so they are red on day one**: `reports/post_close/<date>.md` (stops 2026-07-16), `data/runtime/natural_run_acceptance/<date>.json` (stops 2026-07-16), the morning artifact slots, a journal terminal row per terminal exit.

Absence is a finding of **equal severity** to failure.

Pair it with an **external** liveness proof: the harness's own receipt mtime must be surfaced where a human already looks (the loopback operator console panel). A liveness receipt written by the process that dies proves nothing — verified precedent: `logs/automation.log` line 685399 is `[2026-07-20T14:29:01-0700] === autonomy_watchdog starting ===` and line 685400 is the *next day's* continuity line. No completion, no error. Five days of silence, nobody noticed.

### O8 — Determinism
Two runs over identical inputs must produce an identical terminal classification and identical receipt field values excluding timestamps and run-ids. **Nondeterminism in a fail-closed pipeline is a defect by definition.** Free in a replay-first design; generalizes past any enumerated fault.

### O9 — Suite integrity — and the suite is not the asset it looks like
Measured this month, verify it yourself:
- `python3 -m pytest scripts` **INTERNALERRORs** at `scripts/lib/self_test.py:89` (module-level `raise SystemExit`) and collects **zero** tests. `--ignore=scripts/lib` then dies at `scripts/test_broker_permissions.py:158`; excluding module-level `SystemExit` still dies at `scripts/test_deadline_checkpoint.py:242 sys.exit(0)`.
- Two incompatible dialects coexist. 96 of 154 files shell out via `subprocess`. 37 reference `ROOT/"data"` or `ROOT/"reports"` directly.
- In the first 38 files alone: 5 exceed a 30s cap (`test_adjustment_policy`, `test_autonomy_orchestrator`, `test_autonomy_service_health`, `test_broker_runtime_audit`, `test_candidate_artifact_health`) and 2 are currently **RED** (`test_autonomy_service_watchdog`, `test_event_ledger`).
- (Updated 2026-07-23) A root `conftest.py` now exists — **collection fencing only, no fixtures**: `collect_ignore` lists the 13 standalone lane-2 scripts (module-level `SystemExit` / import-side-effect files) and `collect_ignore_glob` fences `operator_patches/*`, `.claude/*`, `secrets/*`, so `python3 -m pytest scripts sim` collects cleanly (2124 tests, 0 errors at HEAD). The canonical runner is `scripts/run_test_suite.sh` (operator pack `2026-07-23_test_suite_runner`): lane 1 = full-suite one-process pytest (operator console excluded), lane 2 = the 13 standalone scripts one-subprocess-each, lane 3 = `--loopback` operator-console tests. There is still no `pytest.ini`, `setup.cfg`, `pyproject.toml`, `tox.ini`, `Makefile`, or `.github`; no plist invokes pytest, and the only automated trigger remains `.git/hooks/pre-commit`, which runs `build_manifest.py --check` plus **one** test file against the staged index and is bypassable.

Requirements:
- Classify each file by dialect (pytest-collectable vs `python3 <file>` exit-code).
- Record `tests_collected` **and** `tests_passed` per file. **`rc=0` with zero collected is a FAILURE (`vacuous_pass`), not a pass.**
- A per-file **TIMEOUT counts as FAIL**, never as skip/exclude. Lowering a timeout is a coverage-reduction change requiring the human ceremony.
- The must-pass set lives in the **operator-signed lock**, established at signing time. Name the 2 currently-red files and the 5 slow files explicitly as known-red with owners. **A red file may never migrate into the baseline automatically.**
- Until collected-count totals are stable, the repair loop's "suite no worse than baseline" acceptance condition is **disabled**, not satisfied by vacuous passes.
- The suite runs **inside the write-allowlist sandbox, in a SAFE window, never in the hot loop**, with pre/post byte-identity assertions (see O12). It has never once been executed unattended; treat it as an untrusted write surface.

### O10 — Protected-set pinning (the manifest is blind to shrinkage)
`build_manifest.check()` (`:710-719`, `:777-783`) computes drift only over paths **currently in** the hand-written `CRITICAL` literal and never compares stored manifest keys back against that list. Delete a line from `CRITICAL` — say `scripts/freeze_gate.py` — and the path vanishes from `critical`, contributes to neither `drift` nor `unstored`, is never compared to its stored hash, and `--check` prints **PASS with `drift: []`**.

So the harness must independently pin the **set**, every cycle:
- Sorted `CRITICAL` path list and its **cardinality** (597 today) against a signed expectation; report `critical_set_shrunk` naming removed paths.
- `stored_crit` keys ⊇ `CRITICAL`.
- `control_plane_integrity.COMPARISON_EXEMPT_CONTROL_FILES` **exactly** `{"data/system_state.json"}`, plus `CONTROL_TREES` and `CONTROL_FILES` membership.
- The 51 deny entries and 8 allow entries in `.claude/settings.json`.
- `risk_gate.ALLOWED_SECURITY_TYPES`, `DISALLOWED_FLAGS`.
- The gate-semantics lock's own **coverage list** — any gate-shaped module not covered by the lock is a finding.

All of these are **shrink-only**. The applier validator auto-refuses any payload that removes an entry.

**And state this plainly in every report**: a manifest re-sign clears drift and is the terminal step of every applier, so `--check PASS` attests that **a signature exists**, never that semantics are unchanged. Manifest state and gate-semantics state are two independent, separately-labeled facts. A re-sign may **never** clear an outstanding gate finding.

### O11 — Gate monotonicity (behavioral, co-equal with the syntactic fingerprint)
For `freeze_gate.evaluate`, `research_gate.classify`, `research_authority.evaluate`, `risk_gate.validate`, `decision_path`, `coverage_policy`: declare preconditions, postconditions, and a partial order on inputs, then assert **degrading any input may only leave the verdict identical or stricter.**

The gate-semantics fingerprint is a *syntactic projection* and is evadable — by reordering a boolean so a check short-circuits away, by an early return, by moving a check into a sometimes-skipped branch, or by relocating the decisive predicate into a **new module absent from the coverage list**, where the fingerprint does not move at all. Monotonicity is the only proposed check that catches semantic loosening. Treat it as co-equal, never as a supplement.

### O12 — Universal postconditions, after every scenario and every batch
Following the `proof_*.py` house idiom:
- **(a) Production byte-identity**: sha256 before and after of `data/research_ledger.json`, `data/shadow_ledger.json`, `data/MANIFEST.json`, `data/system_state.json`, `data/watchlist.json`, every file under `reports/morning/**`, `reports/post_close/**`, and `data/runtime/broker_audit/**`. Any difference ⇒ the harness **self-fails**, quarantines the batch, and refuses to emit findings.
- **(b)** Gate-semantics fingerprint identical before and after.
- **(c)** PII scan of every produced artifact (emails, 8+ digit runs, tokens, account keys).
- **(d) Self-restraint check**: static scan of the harness's own Tier-A source for any write outside `data/runtime/harness/`, `logs/harness*.log`, `reports/harness/`, `journal/`, `sim/`, `$TMPDIR` scratch — fails the harness closed on a hit. Necessary because the Bash hook never reads file contents and is a speed bump, not a capability boundary.
- **(e) Provenance**: record both conclusions and run `build_manifest --check`. The exact
  `release_snapshot` status is forensic and must remain `drift`/non-reproducible when the complete
  Git-visible state differs. Runtime safety requires `runtime_binding_status` to be `match` or
  receipt-validated `expected_output_drift`, plus manifest `check == 'PASS'`. If the latter status
  applies, assert that every reported difference is bound to its approved output validator and that
  the harness's own cycle introduced none unless a future explicit harness receipt contract allows
  it. Any unsafe/unapproved drift, staged change, protected-source difference, or manifest failure
  writes a self-disable tripwire and refuses all further cycles until a human clears it. The harness
  must never cause a `release_snapshot_not_current` blocker or relabel exact forensic drift as a
  match.
- **(f) Window discipline**: record the harness's own write timestamps and assert **after the fact** that none fell inside any audited child lifetime observed in `data/runtime/broker_audit/<today>/`.

Why (f) matters, and it is not hypothetical: at 2026-07-21T13:50:50 PT, `post_close` was quarantined rc=88 with `changed_control_paths: ['config/quant_strategy_shadow_forward_policy.json']`, and `authority_output_quarantine` took `reports/post_close/2026-07-21.md`, `data/watchlist.json`, and the post_close contract receipt with it. The writer was **an agent session creating a config file**, not a timer. That is exactly what this harness is.

### O13 — Oracle mutation testing — a per-cycle **precondition**
Before evaluating anything real, each oracle runs against a known-bad synthetic artifact and **must go red**. Any oracle that stays green on its own poison pill disables the harness's ability to report green at all for that cycle (`status: oracles_unproven`).

This is the only structural defense against an oracle that asserts the wrong thing — the failure mode that makes every other countermeasure decorative.

### O14 — Contract mining, proposed for ratification
Mechanically derive candidate cross-field implications from co-variation across the receipt corpora (88 health receipts, 8 broker_audit dates) and surface them **for a human to ratify**. Never auto-adopt.

This is the only answer to the deepest limitation of the whole design: **an invariant table only ever contains what someone already thought of.** INC-3 was a contract nobody had articulated. The next failure will be in a seam nobody has named.

---

## 7. Verdicts, noise control, and reaching the human

### Verdict vocabulary — five values, and one of them is not benign
- **`correct_refusal`** — the gate fired for the right reason on the right input. **Requires a matching pre-existing entry in the operator-signed expectations lock.** Without one, the finding stays **open at reduced urgency** — it may not self-close. Legitimate today: the entitlement blackout (`data_readiness_degraded` on 6 of 6 recent intakes; free tier returns HTTP 200 with ~16.8h-old prints; catalyst feed `http_402`; AV intraday premium-only) and the single-source Buy cap with no AV key. Those are **correct behavior and must never be auto-repaired.**
- **`false_positive_refusal`** — a correct guard destroyed a good run. INC-2 and INC-3 both land here. Patchable.
- **`true_defect`** — a real hole, or a contract violation, or no human-visible artifact where one is required. Patchable.
- **`unexpected_success`** — a scenario that **should** have refused and did not. **Most severe class in the system.** Never repairable by the harness. Immediately **freezes the entire proposal loop** and invalidates every outstanding proposal. Escalates human-only. This is the only verdict that detects a gate that has *already* been silently weakened.
- **`flaky`** — quarantined after two disagreeing runs, and the quarantine itself is a tracked finding. Real-process timing injection on a thermally-throttling fanless M1 will be flaky; an ungoverned quarantine list becomes the chronic-red-ignored dynamic.

**There is no unclassified bucket that is quiet by default.** Anything the classifier cannot place is loud.

**Reproduce-twice-or-discard**: a finding is emitted only if a second independent run from the same inputs yields an identical result hash. Non-deterministic ⇒ discarded, never reported.

**False-alarm budget with auto-demotion**: if harness false alarms exceed a set rate, the harness demotes itself from "publishes verdicts" to "silent data collection" until the source is fixed. Credibility is the resource this whole project runs on.

### No unqualified green
Every cycle emits a machine-readable **coverage ledger** alongside its verdict: `artifacts_seen`, `artifacts_checked`, `artifacts_skipped_with_reason`, `contracts_evaluated`, `contracts_not_applicable`, `scenarios_run`, `scenarios_skipped`, `classes_declared_out_of_scope`.

A cycle with a non-empty skipped set reports **`green_with_gaps`**, never `green`. Any renderer or rollup that collapses `green_with_gaps` into `green` is itself a defect the harness must report. **"No finding" and "did not check" must never share a symbol.**

**Schema-less or contract-less is a finding, not a skip.** `data/runtime` carries 95 distinct schema strings and **15 JSON receipts carry no `schema` or `schema_version` at all** — including every `data/runtime/health/<date>_candidate_artifact_health.json` and `data/runtime/health/candidate_artifact_health_rollup.json`, which is the exact artifact sitting at `consecutive_non_healthy_count: 4`. Ship a registry naming every receipt family that **must** carry a contract; emit `uncontracted_artifact` for any match that lacks one.

**Declare the unreachable classes in the output itself, every cycle**: host power/wake (the 2026-07-14 full-day loss; `pmset -g sched` shows "wakepoweron at 4:55AM weekdays only"; repair needs `sudo pmset` and is human-only), launchd scheduling semantics, bash-3.2 quoting bugs, Seatbelt profile *generation*, live provider contract drift, Claude CLI version drift breaking the regex table in `classify_claude_failure.py`, and external agents mutating the working tree. **Silence over a declared-unreachable class must be visibly different from silence over a checked one.**

### Fixture and scenario decay
Every fixture record embeds the **git sha, the working-tree dirty set, and the sha256 of every producing module at capture time**. Every scenario declares `mode`:
- `stored_bytes` — asserts a historical artifact's shape. Red or green for N cycles with no producing-module change ⇒ reported **NON-INFORMATIVE**.
- `regenerated` — asserts current code output. If the producing module's sha changed since capture ⇒ automatically **STALE**, cannot count toward green until re-derived and re-ratified.

Golden-red fixtures must be paired with a **positive control** that goes green only under fixed code. Otherwise: the 2026-07-21 receipt can never be regenerated by current code (`comparison_digest()` now hashes the exempt-filtered projection), so asserting stored bytes is red forever independent of code, and asserting current output is green forever independent of the fixture. A corpus of post-fix recordings is a corpus of tautologies, and its uniform green is indistinguishable from health.

### Simulated artifacts are segregated and poison-tagged
Every artifact any simulation produces carries `simulated: true` plus the scenario id, is written **only** under the scratch root, and the real-corpus oracle **refuses to ingest — and raises a P0 finding on encountering — any artifact bearing that tag or found outside that tree.** Otherwise the harness verifies its own output and reports the pipeline consistent.

Note `scripts/research_authority.py:382` honours injected values under `trust_model_inputs` and stamps it into output at `:448` — a marker nothing downstream is required to reject. Reject it.

### Findings are append-only and unrewritable from the model side
`.claude/settings.json` grants `Edit(reports/**)`, `Edit(journal/**)`, `Edit(data/**)`. So:
- Findings go to a **per-line hash-chained JSONL** whose head is anchored in the operator-signed lock.
- The harness never rewrites or deletes a prior line.
- A transition to `resolved` requires the scenario id from the signed lock **plus** the applier receipt sha.
- The human-readable report is a **render** of the chain, never the record of truth.

### No model-writable suppression surface
**No** `known.json`, quarantine list, mute list, or dedupe-fingerprint file may live under `data/runtime/`, `sim/`, `journal/`, or `reports/`. Suppression requires a **human-signed entry with a mandatory expiry**, and the count of currently-suppressed findings plus their ages is printed at the top of every human-facing report. **"Already known" may never silence a finding.**

Same for BUSY/blackout policy and per-test timeouts: operator-signed config, never the model-writable tier. Count every skipped cycle; k consecutive skips is its own finding; report weekly total skipped time.

**Coverage is a tracked quantity.** Any coverage reduction — disabling a scenario, narrowing a predicate, dropping a module from lock coverage, shrinking a property-test input space, raising a timeout — requires the same human ceremony as a gate change and appears as a **coverage delta** in the weekly rollup.

### Reaching the human
1. **Stage 0 ships the notification pack.** Minimal, scoped to pipeline integrity only.
2. `reports/harness/` (human-readable render) + `journal/harness_findings.jsonl` (the chain) + `journal/improvement_backlog.json` in its existing schema.
3. A **read-only `/sentry` panel** in `scripts/operator_console.py` — already loopback-only on `127.0.0.1:8787`, pull-based, 15s cache, `mutation_endpoints_enabled: false`, KeepAlive for days with zero incidents. It must surface the harness's own liveness timestamp.
4. **Silence discipline**: never per-cycle, never per-finding. Silent on green. **At most one** human-facing signal per day, and only on the **first transition** of a gate to red. Escalation is by **consecutive-failure count**, not instantaneous label. The demonstrated pathology is a red rollup sitting unread for four or five sessions — and the demonstrated risk of the opposite is that a user who dismisses trading-system banners stops seeing the real morning failures.
5. **Publication happens BEFORE classification**, so the harness's own refusal paths still emit. "Observer downstream of a fail-closed exit" is exactly the anti-pattern at `run_autonomy_watchdog.sh:117-121`, and it is a finding class the harness reports.

---

## 8. Acceptance criteria

### 8.1 The four-incident replay — the primary gate
For each of INC-1..INC-4: **the harness must fail RED against the pre-fix code and pass GREEN against the post-fix code.** Both directions are required; a scenario that is red under both is measuring nothing.

Concrete requirements per incident:

- **INC-1 (rc=124)**: red when measured stage latency from `data/runtime/health/<date>_timeline.json` exceeds the `config/research_budget.yaml` allocation — using the **07-08 and 07-10 timelines**, which is nine days of lead time. Independently, the injected-latency scenario asserts the deadline checkpoint fires strictly before `latest_freeze_pt 06:20` and writes **exactly one** labeled preliminary.
- **INC-2 (rc=88)**: red the moment the lattice check runs against a plist set where a `record_state()` caller ticks inside `[morning_start, morning_start + p95]`. Independently, `sibling_control_write` against a **real scratch root with real concurrent processes** must produce rc=88 for a non-exempt path and **not** for `data/system_state.json`.
- **INC-3 (rc=90)**: O1 must flag the stored 2026-07-21 receipt **without importing `model_integrity_binding`** — proving the *class* is caught, not the incident. O2 must independently catch it as a producer/consumer domain mismatch by sweeping the exemption matrix through the real `compare_control_plane` in a real scratch root. And the Stage-3 clamp, seeded with the actual 07-20 exemption diff, must **refuse it as a proposal**.
- **INC-4 (exit 78)**: the temporal differ must raise **before** the lane fires, naming the drifted files and the minutes-until-fire. The absence registry must independently flag `reports/post_close/` and `data/runtime/natural_run_acceptance/` as stale.

### 8.2 Standing conditions, checked continuously
- `execution_enabled == false` asserted on every artifact the harness inspects and carried in a `SAFETY` block copied verbatim from `morning_dag.py:62-76`.
- O12(a) byte-identity holds after every batch, every time.
- O13 mutation self-test green, including the four planted weakenings: **add a member to `COMPARISON_EXEMPT_CONTROL_FILES`; insert an `os.environ` early-return before `research_freeze.py:1300`; flip a comparison polarity in `freeze_gate`; delete one `raise`.** If **any** planted mutation does not move the fingerprint, the harness refuses to run and says so loudly.
- Self-attest before every cycle, halt loudly on failure: `build_manifest --check` PASS; gate fingerprint == signed lock; expectations lock hash == signed value; every Tier-A file present in `data/MANIFEST.json` `critical_sha256`; all exemption/allow-set cardinalities and memberships exact; O13 green. **Refusal must never be indistinguishable from a green day.**

---

## 9. Anti-gate-laundering — mechanical, not aspirational

The history proves the temptation is real: the 07-20 fix relaxed a comparison scope and directly caused rc=90 the next morning.

### The three-key clamp
A repair may be drafted as an applier **only if all three hold simultaneously**, machine-checked, no human judgement in the path, **no override flag**:

- **KEY 1** — the recorded scenario flips red→green, and its ledger entry **predates** the draft.
- **KEY 2** — every existing must-pass test stays green **and** every other harness scenario stays green, run in a fresh scratch root against the patched copy. (Disabled until O9 collected-counts are stable — do not satisfy it with vacuous passes.)
- **KEY 3** — the gate-semantics fingerprint is **byte-identical** before and after, **and equal to the value in the operator-signed lock**.

The fingerprint is stamped into **every observation and every proposal**. A mismatch between observation-time, proposal-time, and the signed lock **invalidates all outstanding proposals** and forces re-observation, so a weakening cannot be laundered through a stale finding.

### The gate-semantics fingerprint — specify it exactly, do not improvise
Normalized **semantic projection**, not raw bytes, modeled on `broker_runtime_audit.policy_sha256()` (`:235-243`). Projection contents, minimum:
- Sorted multiset of every failure-reason string literal (`failed.append(...)`, `reasons.append(...)`, block-reason literals).
- Every module-level constant **by name**: `freeze_gate.MAX_STAMP_SKEW_MINUTES` (15), `research_authority.EXECUTION_ENABLED` (False, `:53`, "permanent; asserted by tests; never configurable"), `decision_path.STAGES` (12) and `ACTIONABLE_CLASS` ('Buy'), `risk_gate.DEFAULTS` / `_TIGHTER_IS_LARGER` / `ALLOWED_SECURITY_TYPES` / `DISALLOWED_FLAGS`, `control_plane_integrity.CONTROL_TREES` / `CONTROL_FILES` / `COMPARISON_EXEMPT_CONTROL_FILES`.
- Count **and polarity** of comparison sites (`is not True`, `!= 'PASS'`, `is not False`).
- **AST-level count of early returns and branch depth per gate function.**
- **sha256 of each covered file's normalized AST dump.**
- `config/governance.yaml` `buy_requires` (`:89-96`) and `publication_safety_overlays` (`:23-34`), active version (`:19`, v2.0).
- The **number of call sites** honoring `research_authority.evaluate(trust_model_inputs=True)`.

Coverage list, minimum: `scripts/freeze_gate.py`, `research_gate.py`, `research_authority.py`, `decision_path.py`, `lib/risk_gate.py`, `control_plane_integrity.py`, `model_integrity_binding.py`, `coverage_policy.py`, `config/governance.yaml`, `config/risk_limits.yaml`, `config/source_authority.yaml`, and **both hook copies**. The coverage list itself is inside the signed lock; dropping a module from it is a forbidden change.

Frozen into `config/gate_semantics.lock.json` — operator-delivered, in `CRITICAL`. **The harness cannot re-baseline the lock.**

### Forbidden-change class — no override path, ever
The applier validator runs this on its **own payload before writing anything**. Auto-refuse, logged (mirroring `risk_gate`'s `ignored_loosening` ledger so refusals are **recorded**, not silently dropped):

- (a) Any diff touching a path in the gate lock, in `CRITICAL`, either hook copy, `.claude/settings.json`, `.codex/hooks.json`, `scripts/build_manifest.py`, any `launchd/*.plist`, the expectations lock, or the fixture lock.
- (b) Any removal or rename of a failure-reason string literal.
- (c) Any numeric change to `config/risk_limits.yaml` in the **loosening** direction, classified with `risk_gate._TIGHTER_IS_LARGER` (`:36-37`) as the direction table. **Note the real hole**: `effective_limits` (`:100-128`) applies tighten-only to **caller-supplied** limits, but values loaded from `config/risk_limits.yaml` overwrite `DEFAULTS` **unconditionally**. Extend the same treatment to `config/governance.yaml` `buy_requires` and `publication_safety_overlays`, `config/source_authority.yaml`, and `config/research_budget.yaml` (any deadline or budget increase that moves work past `total_deadline_pt 06:10` / `latest_freeze_pt 06:20` is loosening).
- (d) Any change to `execution_enabled`, `EXECUTION_ENABLED`, `buy_requires`, `publication_safety_overlays`, `decision_path.STAGES`, `ACTIONABLE_CLASS`, `ALLOWED_SECURITY_TYPES`, `DISALLOWED_FLAGS`.
- (e) Any widening of `COMPARISON_EXEMPT_CONTROL_FILES`, `CONTROL_TREES`, `CONTROL_FILES`, or the 51 deny entries in `.claude/settings.json`.
- (f) Any widening of where `research_authority.evaluate(trust_model_inputs=True)` is honoured (`research_authority.py:323`, `:382`, `:503-505`), or any change to the `canonical_production` expression / the `canonical_freeze_test_injection_forbidden` raise (`research_freeze.py:1206-1209`, `:1296-1300`). **These are frozen at their current shape and call-site count, by name and line.** The harness needs the injection knob, which makes it the single most dangerous line in the design. **Any scenario blocked by that raise must be re-engineered, never unblocked.**
- (g) Any deletion, skip, `xfail`, rename, or assertion-removal in an existing `scripts/test_*.py`. **Existing tests are append-only.** The harness may ADD a test file via an applier; it may never edit one. The validator diffs every test payload against the on-disk file and auto-refuses on assertion removal. Tests here encode policy, not just behavior — `scripts/test_free_source_health_alert.py:54` asserts `external_notification_enabled` is False.
- (h) Any weakening of a pre-commit WARN-and-continue branch into a broader skip (`scripts/git_hooks/pre-commit:8`, `:11-22`, `:25-34` — it already degrades open in three places).
- (i) Any patch introducing an `os.environ` / `os.getenv` read **inside a gate module** (AST check). Stamp the full sanitized environment of every run — harness and production — into its receipt. An env-gated bypass is auto-rejected regardless of what the fingerprint says.
- (j) Any change to the gate-semantics normalizer itself, the mutation corpus, the expectations lock, or the fixture lock.

For anything in (a)–(j) the harness emits **only** a failing scenario plus a written argument. It never drafts the patch. **There is no `--force`, no config key, and no "the harness needs this to work" escape — and that justification is itself the attack signature.**

### Prove the clamp continuously
Seed a disposable copy with the **actual 2026-07-20 exemption diff** plus the four planted mutations, and require the harness to catch every one **as a precondition to publishing any green verdict**. A clamp that has never been observed to fire is not evidence.

---

## 10. What the harness must NEVER do

Violations of these are obvious by inspection. Any design element that requires one is disqualified.

1. **Never** place, modify, cancel, preview, or simulate a live order. Never touch a broker tool, MCP or otherwise. `execution_enabled` is permanently false and is asserted, not configured.
2. **Never** write `reports/morning/**`, `reports/morning/official_research/**`, `data/research_ledger.json`, `data/shadow_ledger.json`, any `freeze_receipt.json` / `legacy_freeze_receipt.json`, `data/MANIFEST.json`, or anything under `data/runtime/{broker_audit,preflight,robinhood,rh_candidate_quote_raw}/`.
3. **Never** write any byte under `scripts/`, `config/`, `prompts/`, `schemas/`, `launchd/`, `.claude/`, `.codex/`, `data/runtime/research/.publisher_locks` — at runtime, from any process, for any reason, including a lock file, a temp file, a cache, or a rendered plist. `restore_control_plane` (`:360-414`) will move it to quarantine and rewrite the pre-child bytes over it, **and** classify the run rc=88.
4. **Never** acquire, create, or remove `.run.lock`.
5. **Never** call `record_state()`, `notify()` per cycle/finding, `caffeinate`, `pmset`, or `sudo`.
6. **Never** put a `YYYY-MM-DD` token in any artifact path relative to `data/runtime/`.
7. **Never** monkeypatch `ROOT` or `CODE_ROOT` in-process to drive `research_freeze.py` / `freeze_report.py`. It disables the injection guard rather than satisfying it.
8. **Never** invoke the production `audited_claude.py` with `--root` pointing elsewhere — that shape runs the child with **no** Seatbelt profile.
9. **Never** SIGKILL by name, pattern, or `pgrep`. Own process group, by pgid, only.
10. **Never** run a git mutation outside an explicitly-bound scratch `GIT_DIR`/`GIT_WORK_TREE`, and never without the refusal precondition that the resolved work tree is not the production root.
11. **Never** make a network call or hold a credential in the continuous tier. There is **no** FMP rate-limit or quota config anywhere in the repo, and the only metered mechanism (`scripts/av_budget.py`, 25/day) has been dead since 2026-06-23. The guardrail is **absence of capability**, not a budget file.
12. **Never** author a patch in the forbidden-change class (§9), and never "fix" a red result by editing an expectation, a fixture, a severity, a contract predicate, a suppression list, a baseline, or a timeout.
13. **Never** self-close a finding as `correct_refusal` without a matching entry in the operator-signed lock.
14. **Never** report `green` when anything was skipped. **Never** let "did not check" and "no finding" share a symbol.
15. **Never** keep running while broken. If any per-cycle postcondition fails — byte-identity, release-status match, manifest `--check` PASS, sandbox unavailable, scratch root outside `$TMPDIR`, a date token in an artifact path, an oracle failing its poison pill — write a tripwire and have every subsequent invocation exit 0 immediately until a human clears it.
16. **Never** add trading velocity, research breadth, additional data sources, faster cadence, or louder notification of trading content. This is pipeline diagnostics only.

---

## 11. Non-goals

- Not a second production pipeline, a mirrored filesystem root, or a second launchd estate. The estate is already 8 jobs on a fanless 8-core M1 with ~12 minutes of slack in the freeze window; doubling it manufactures the availability class it would exist to prevent.
- Not a research-quality improver. Source independence, the single-source Buy cap, and the entitlement blackout are **correct** constraints, not defects.
- Not faster or louder trading. Not an execution engine. Not a notification firehose.
- Not a replacement for the 154-file suite, `morning_dag.py`, `natural_run_acceptance.py`, `premarket_simulation.py`, the `proof_*.py` harnesses, the four selftest shells, `rehearse_session.py`, or `replay_session.py`. Extend them.
- Not a fixer of host power/wake (`sudo pmset`, human-only), launchd semantics, bash-3.2 quoting, or an external agent mutating the working tree. **Declare these unreachable in every cycle's output.**
- Not a self-healing system. The honest ceiling is: **earlier, clearer, human-readable detection, plus a narrow class of mechanically-fenced fix proposals.** The highest-cost classes (INC-4's manifest ceremony, INC-2/INC-3's envelope collisions) are all gate-adjacent and route to a human-written argument by design. Do not promise otherwise in any report the harness generates.

---

## 12. Order of work

1. **Stage 0 pack** — applier authority gate (with `--plan` carve-out), tty confirmation, harness authority-surface exclusion, declared-change re-signing, notification reachability, `.gitignore` additions. Nothing else ships first.
2. **Stage 1 under `sim/`** — read-only observer, no operator pack. Must produce the Stage-1 findings list on real data.
3. **Fixture promotion** — PII-scrubbed real receipts into tracked storage, with provenance records. Week one; the corpus is one `git clean` from gone.
4. **Stage 2 pack** — Tier-A promotion, scratch roots, real-process scenarios, exit-code table, suite runner, `CRITICAL` appends + re-sign.
5. **Stage 3 pack** — gate-semantics lock, mutation corpus, monotonicity, forbidden-change classifier, three-key clamp, proposal loop, `/sentry` console panel.

Before **any** commit ceremony or manifest re-lock, re-verify HEAD and the dirty set. Other Claude and Codex sessions commit to this same working tree mid-session; an ambiguous anchor must abort the applier, not be resolved by guessing.
