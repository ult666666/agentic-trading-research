# Trading-system completion roadmap

Status date: 2026-07-21
Operating mode: research-only; manual orders only; execution permanently disabled

## What “finished” means

This project is not finished merely because it can produce a report or pass unit tests. It is
finished enough for disciplined use only when all of the following are true:

1. every material research claim is bound to independently revalidated source bytes;
2. every candidate price is fresh, session-correct, liquid, and independently cross-checked;
3. the strategy has a prospective, future-only sample with modeled costs and a complete session
   denominator;
4. the sample is large enough to estimate expectancy, drawdown, false positives, and regime
   sensitivity without optional stopping;
5. the natural scheduled workflow completes repeatedly without integrity, freshness, or broker
   audit failures;
6. a human can reproduce every conclusion from immutable inputs and receipts; and
7. no component can place an order, loosen risk, or claim guaranteed profit.

No software can make future profit certain. The measurable goal is positive risk-adjusted
expectancy after realistic costs, with bounded downside and enough out-of-sample evidence to make
the estimate credible.

## Current completion gates

| Gate | Current state | Exit criterion | Authority while open |
|---|---|---|---|
| Source proof | Production validator registry intentionally empty; the complete-denominator receipt manifest exists only in shadow mode | Trusted control-plane collector and registered validator bind exact target, claim, source role, immutable response bytes, and same-run provenance; adversarial tests pass | No positive catalyst authority; Watch/No-Trade only where allowed |
| Point-in-time data | Feature corpus has no eligible rows; broad daily history, corporate actions, delisted coverage, and adjustment semantics are missing | Licensed, survivorship-aware, point-in-time corpus with documented retention rights, reproducible snapshots, and readiness PASS | No reliable backtest or profitability inference |
| Prospective evidence | Shadow monitor and a caller-dated standalone stateful forward engine exist; the engine's roster is self-committed but its time and plan approval are not externally proven, their contracts are not yet joined, and no approved real plan or future sample exists | Approval-bound replay intent for every strategy, trusted-clock canonical plan/session receipts, monitor migration to the complete forward-run denominator, human-approved future plan, fixed checkpoints, and sufficient sample | Research diagnostic only |
| Natural operations | LLM ensemble and broker audit have deterministic tests, but natural-run acceptance remains incomplete | At least five consecutive valid-session canaries pass every required integrity, freshness, provenance, and zero-write check; no unexplained missing terminal | Fail closed; no silent promotion |
| Economic validation | Score calibration and research factory have zero eligible observations | Pre-registered evaluation reaches minimum sample; after-cost expectancy, profit factor, drawdown, confidence bounds, and regime slices meet fixed thresholds | Existing score remains uncalibrated |
| Release integrity | All active tests are green under their correct runners and the signed manifest is re-locked at HEAD (`build_manifest.py --check` PASS, 622 critical files), but the release snapshot (`release-20260717-prewake-red-debt`, head 2e8e799) is 6 days stale and still predates this fortification work | Clean signed manifest, all required tests green, immutable release snapshot verifies against current commit, and rollback receipt exists | Development tree is not a release |

## Current verification baseline

The 2026-07-21 fortification baseline is green across every active test file:

- 2,066 pytest checks passed and 6 conditional integration checks skipped;
- 24 loopback HTTP checks passed in an environment permitted to bind `127.0.0.1`;
- 469 legacy standalone checks passed through their intended direct runners; and
- 125 top-level self-checks passed for the three files that intentionally collect zero pytest
  cases.

The suite also found and closed two order-dependent test leaks: a temporary entitlement-cache
path and the process `PATH` were not restored after destructive fixtures. Those fixtures are now
transactional, and preflight decisions are isolated from the real runtime entitlement cache. The
baseline is development evidence only until the human-controlled manifest re-sign and release
snapshot gates pass.

## Ordered build plan

### Milestone 1 — close false authority paths (shadow foundation implemented)

- Keep the catalyst validator registry empty until exact semantic claim receipts exist.
- Build the source-receipt manifest as `shadow_only` and prove complete denominators, missing
  receipts, unsupported sources, non-allowlisted sources, and orphan receipts.
- Do not connect this shadow manifest to evidence quorum, Buy authorization, freeze, publication,
  metrics, or execution.

Definition of done: all receipt artifacts are create-once, hash-bound, denominator-complete, and
incapable of granting authority.

### Milestone 2 — generate reproducible session-by-session results (standalone engine implemented)

- Require an executable replay specification with a shared caller-frozen strategy roster. The
  current library records a caller-declared creation time; only a future trusted outer receipt can
  prove that it was frozen before the first observation.
- Advance normalized capital one valid market session at a time.
- Carry positions and cash across sessions; charge entry and exit costs once.
- Model daily-bar ambiguity conservatively: gap-through-stop and stop-first when stop and target
  are both touched.
- Maintain a continuous SPY comparison instead of restarting it every day.
- Distinguish a verified zero-signal session from unavailable or conflicting data.

Definition of done for the standalone library: candidate, benchmark, and every incumbent named by
the shared roster has exact prior-state and input hashes, deterministic state continuity, and
adversarial validation tests. The roster catches later subset deletion but is not plan or approval
proof and cannot stop a same-user rebuild of the full spec set. The engine remains unscheduled
until a real eligible dossier, approval-bound plan, trusted clock receipt, and explicit human
approval exist.

Before this engine can feed the existing monitor, one additional engineering contract is required:
every candidate, benchmark, and incumbent must have a validated replay-intent source containing
the full variant parameters and simulation assumptions. The current historical
`strategy_contract/1.1` does not contain those fields, so copying defaults would silently change
strategy identity. The eventual plan also needs an approval receipt that binds the human decision
to the exact plan hash; a free-form approval reference is not cryptographic approval.

### Milestone 3 — obtain lawful point-in-time data

- Do not use the current FMP entitlement to create a prohibited bulk or long-lived historical
  corpus.
- Select a provider/license that permits local retention, derived research artifacts, corporate
  actions, delisted securities, and survivorship-aware history.
- Backfill through the existing immutable data-lake contracts, then run readiness, capture,
  feature, label, grid, factory, cohort, uniqueness, and evidence gates without bypasses.

Definition of done: every training and evaluation row is reproducible as-of its decision time and
the data-rights record explicitly permits the retained use.

### Milestone 4 — collect prospective evidence

- Require an eligible evidence dossier and a separate explicit human approval before creating a
  shadow plan.
- Record every closed session as complete or unavailable; never backfill a favorable zero.
- Review only at pre-registered checkpoints and preserve the full incumbent denominator.
- Compare after-cost expectancy, drawdown, hit rate, profit factor, capacity, and regime stability.

Definition of done: the fixed minimum sample and confidence bounds in the frozen policy pass. A
supportive result permits human review only; it never auto-promotes a strategy.

### Milestone 5 — prove unattended reliability

- Run natural morning, post-open, post-close, watchdog, and broker-audit canaries.
- Reconcile every terminal artifact, journal event, receipt, and release snapshot.
- Treat concurrent control-plane edits as a release-process failure. Before changing protected
  scripts, configuration, schemas, prompts, or manifests, verify that no audited production child
  is active; serialize release work with the existing audit/lock namespace.
- Investigate every rc=86/87/88/89/90 event by exact receipt, not by prose inference.

Definition of done: the acceptance window completes without drift, stale data, missing artifacts,
broker writes, or unexplained recovery behavior, and rollback is tested.

## Hard blockers that code alone cannot remove

- lawful access to adequate point-in-time market and corporate-action history;
- elapsed future market sessions for a prospective sample;
- explicit human approval for a specific shadow plan;
- sufficient observations to estimate performance with honest uncertainty; and
- real market regimes that cannot be manufactured or accelerated by software.

These blockers are not excuses to weaken a gate. While they remain, the system should keep
collecting evidence, surface the exact missing condition, and prefer No-Trade to unsupported
confidence.

## Change-control rule

Every completed milestone must update the model inventory, linked tests, integrity manifest,
system-change journal, and release snapshot. A milestone is not complete when code merely exists;
it is complete only when its tests pass, documentation matches runtime behavior, production
authority is unchanged unless separately approved, and the next rollback condition is explicit.
