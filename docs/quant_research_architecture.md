# Quant Research Architecture

Status: current as of 2026-07-21 (quant stack plus point-in-time feature/label contracts).
Authority: [`AGENTS.md`](../AGENTS.md) is the project constitution and **overrides this
document everywhere**. This file is a map, not a rulebook — when a rule matters, it links to
the constitution section or the deterministic script that enforces it instead of restating it.

---

## 1. What this system is (and is not)

A **research-only** pipeline that produces catalyst-driven swing-trade research for a human
who places every order manually ([`AGENTS.md §0`](../AGENTS.md), [`§1`](../AGENTS.md)).
Execution is permanently disabled (`execution_enabled: false` in
[`config/governance.yaml`](../config/governance.yaml); broker write tools deny-listed per
[`AGENTS.md §11`](../AGENTS.md)). Instrument scope, fail-closed data rules, and the
"missing/stale/conflicting/malformed ⇒ NO TRADE / WATCH" rule are constitution §1 — nothing
in this architecture may weaken them.

## 2. Intake and morning pipelines (current)

At 05:00 PT, [`scripts/run_data_intake.sh`](../scripts/run_data_intake.sh) builds an immutable
point-in-time snapshot and readiness receipt through the same supervisor. It invokes no model and
does not change production source authority. See [`docs/data_lake.md`](data_lake.md).

Entry point: launchd 05:38 PT → [`scripts/run_morning_scan.sh`](../scripts/run_morning_scan.sh).
When the repository LaunchAgent's opt-in is installed, that runner first delegates to
[`scripts/autonomy_orchestrator.py`](../scripts/autonomy_orchestrator.py), which supervises the
existing runner without replacing any data, evidence, risk, freeze, or ledger authority. See
[`docs/autonomy_orchestration.md`](autonomy_orchestration.md).

The production sequence below remains authoritative. A deterministic replacement controller now
exists in [`scripts/morning_dag.py`](../scripts/morning_dag.py), but version 0.1 is shadow-only,
disabled by default, and writes no production report, ledger, or candidate artifact. Its absolute
stage deadlines, read/web-only worker surface, process-group cancellation, and promotion gate are
documented in [`docs/morning_dag.md`](morning_dag.md).

Stage sequence (each stage emits a timeline event, see step 11):

```
launchd 05:38 PT
  └─ run_morning_scan.sh
      └─ autonomy_orchestrator.py (opt-in: config/integrity/state/deadline/postconditions)
          └─ run_morning_scan.sh (child marker; existing deterministic, fail-closed workflow)
      1. lock + trading-day gate ............ scripts/validate_market_day.py
      2. broker-permission validation ....... scripts/validate_broker_permissions.py
      3. stage decision ..................... scripts/stage_decision.py
         (late/no-invoke ⇒ the model is NEVER called — Layer-0 invariant)
      4. deterministic data preflight ....... scripts/data_preflight.py
         (same-day snapshot + vintage stamps; blackout ⇒ labeled Preliminary No-Trade)
      5. Robinhood READ-ONLY collector ...... scripts/run_robinhood_readonly_collector.sh
         → scripts/validate_robinhood_snapshot.py → scripts/compare_price_sources.py
         (restricted low-cost collector-model use is reported explicitly)
      5a. both live authorities unavailable . scripts/deterministic_no_live_terminal.py
         (hash-bound PRELIMINARY_PRICE_UNAVAILABLE; skip the main research model + ensemble)
      6. governance probe + model pin ....... scripts/select_main_model.py
         (config/model_policy.yaml: Sonnet-5 primary, Opus-4-8 startup-only fallback,
          Fable family refused for unattended production)
      7. bounded `claude -p` invocation ..... prompts/morning_scan.md (protected)
         └─ ENSEMBLE fan-out (parallel, artifact-per-agent, AGENTS.md §4):
            market-state / catalyst-discovery / sec-ir-verification / macro-event /
            technical-historical / portfolio-risk / skeptic
         └─ ADJUDICATION: scripts/deterministic_adjudicator.py → evidence.json,
            validated by scripts/evidence_schema.py (quorum, syndication dedupe, conflicts)
         └─ THREE AUTHORITIES: scripts/research_authority.py
            (live-price / official_research_eligible / buy_authorized — independent;
             AGENTS.md §3b; see docs/data_authority_matrix.md)
         └─ SYNTHESIS: final-strategy-synthesizer → draft.md (renders derived state only)
         └─ FINAL MODEL MUTATION: exact run/content-bound freeze_request.json; the model never
            invokes a publisher or writes a report, ledger row, or terminal receipt
      8. trusted outer publication ............ scripts/freeze_request.py + research_freeze.py
         (only after audited model exit 0 and same-run wrapper/broker/control-plane provenance
          revalidation; used for BOTH fmp_primary and robinhood authority; official_research slot,
          receipt/source/evidence-anchored, prose-consistency contract). New canonical v1
          Official/appendix writes are permanently disabled; scripts/freeze_report.py is a
          fail-closed adapter only.
         └─ DIAGNOSTIC EVENT GRAPH: scripts/catalyst_graph.py
            (exact symbol + claim_ids joins, source-origin dedupe, conflicts, immutable hash;
             never feeds Buy/scoring/risk or changes the completed freeze)
      9. rc=124 / duplicate guard ............. scripts/freeze_completion_probe.py
         (freeze-before-kill ⇒ completed safely; `--official-only` re-anchors canonical path,
          report/evidence/live-source hashes and authority before any duplicate-run early exit;
          genuine timeout ⇒ scripts/timeout_checkpoint.py)
     10. terminal reconcile ................... scripts/terminal_reconcile.py
         (EXACTLY ONE accepted terminal artifact per target session; duplicates quarantined
          to reports/morning/preliminary/superseded/, never deleted; 2026-07-08 incident class)
     11. observability ........................ scripts/run_timeline.py
         (data/runtime/health/<target>_timeline.json — statuses/paths only, never balances)
         + scripts/morning_health.py, receipt-anchored classification, ledger append,
         + scripts/run_diagnostics_emit.py (atomic diagnostics on EVERY classified terminal path;
           it never advances an issue lifecycle before finality)
         + scripts/morning_audit_supervisor.py (receipt-valid audit -> exactly-once,
           runtime-only iteration projection; tracked improvement history remains operator-owned)
```

Ledgers: [`data/research_ledger.json`](../data/research_ledger.json) (governance-v2, append-only)
and [`data/shadow_ledger.json`](../data/shadow_ledger.json) (v1, never rewritten) — see
[`AGENTS.md §3b`](../AGENTS.md). Report stages and the freeze-gate rules are
[`AGENTS.md §3a`](../AGENTS.md); the model-side contract is
[`prompts/morning_scan.md`](../prompts/morning_scan.md) (protected, read-only).

## 3. The locked decision path

[`scripts/decision_path.py`](../scripts/decision_path.py) locks the 12 production stages
(constitution [`§3a` "Decision path is locked"](../AGENTS.md)):

```
broad_discovery → eligibility_filtering → candidate_enrichment → source_verification →
specialist_review → scoring → risk_veto → final_classification → account_executability →
workflow_quality_gate → freeze_gate → shadow_ledger_write
```

No candidate may skip a stage or be inserted post-hoc. Only a final-Buy survivor (no veto,
risk gate PASS, all stages traversed) may enter executable fill simulation; everything else is
a diagnostic price path excluded from performance. 2026-07-08 hardening (see the module
docstring): a **missing** risk-gate result blocks, veto status must be exactly `False`, and the
full-path requirement is no longer candidate-controlled.

Scoring is [`scripts/score_candidate.py`](../scripts/score_candidate.py)
([`AGENTS.md §7`](../AGENTS.md)); the mandatory sizing/risk gate is
[`scripts/lib/risk_gate.py`](../scripts/lib/risk_gate.py), whose limits authority is the
protected [`config/risk_limits.yaml`](../config/risk_limits.yaml) — callers may only tighten
(locked by [`scripts/test_risk_gate_limits.py`](../scripts/test_risk_gate_limits.py)).

## 4. The three authorities (governance v2)

Defined in [`config/governance.yaml`](../config/governance.yaml) (v2.0 active), computed
deterministically by [`scripts/research_authority.py`](../scripts/research_authority.py),
summarized in [`AGENTS.md §3b`](../AGENTS.md), rendered human-readably in
[`docs/data_authority_matrix.md`](data_authority_matrix.md). One sentence each:

* **Live-price authority** — which validated source supplies the current research price
  (`fmp_primary` | `robinhood` | `none`, fail-closed).
* **`official_research_eligible`** — the research *process* completed with adequate
  provenance (authority + validated evidence artifact + receipt-backed catalyst quorum when a
  catalyst is asserted + risk review). The production receipt-validator registry is currently
  empty, so direct web/SEC/IR findings remain leads.
* **`buy_authorized`** — the unchanged v1 Buy gate; a stale FMP primary always blocks Buy.

None implies another; Official Research NEVER implies Buy; model prose can never override any
of them.

[`scripts/source_receipt_manifest.py`](../scripts/source_receipt_manifest.py) adds a shadow-only
completeness layer beneath source adjudication. It enumerates every SEC/news discovery unit,
distinguishes file presence from receipt-contract validity and exact unit identity, exposes
missing/orphan/malformed states, and publishes create-once canonical bytes with narrow interrupted-
commit recovery. It grants no catalyst authority: the production validator registry remains empty
until a trusted same-run claim/source-byte bridge exists. See
[`docs/source_receipt_manifest.md`](source_receipt_manifest.md).

## 5. Where the quant lab and outcome diagnostics sit

`scripts/quant/` is the **offline research lab** (Layer 3 of the readiness report). The
point-in-time and outcome diagnostics layer now sits beside it:
[`scripts/point_in_time_bars.py`](../scripts/point_in_time_bars.py),
[`scripts/outcome_tracker.py`](../scripts/outcome_tracker.py), and
[`scripts/research_attribution.py`](../scripts/research_attribution.py). Report-level governance-v2
ledger rows can also be expanded by
[`scripts/report_candidate_extractor.py`](../scripts/report_candidate_extractor.py), which parses
only known report sections and keeps extracted candidates diagnostic-only. Their position in the
architecture is deliberate and non-negotiable:

* **Offline.** Nothing in the morning-run pipeline (section 2) imports or invokes it; it runs
  on the replay corpus, explicit fixtures, and locally accumulated validated history only.
* **Diagnostics-only.** Every output is labeled a diagnostic, excluded from win rate /
  expectancy / official replay outcomes — the same posture as
  `decision_path.diagnostic_record()` and [`data/replay_ledger.json`](../data/replay_ledger.json).
  Attribution output says "research diagnostic, not executed trading performance."
* **Source ledgers are read-only.** Outcome tracking reads [`data/shadow_ledger.json`](../data/shadow_ledger.json)
  and [`data/research_ledger.json`](../data/research_ledger.json), then writes a derived artifact
  under `data/runtime/`; it never rewrites ledger history. When a research-ledger row points at a
  report instead of symbol-level records, the report extractor may derive symbol-level diagnostic
  candidates from bolded list bullets, but no-trade prose never fabricates candidates and all
  extracted rows keep `counts_in_metrics=false`.
* **No activation authority.** A quant-lab result can *propose* a change; it can never
  *activate* one. Activation requires category-D change control (replay evidence + explicit
  approval + [`journal/system_changes.md`](../journal/system_changes.md) entry per
  [`AGENTS.md §12`](../AGENTS.md)) — see
  [`docs/replay_and_backtest_plan.md`](replay_and_backtest_plan.md).

The unattended extension is documented in
[`docs/research_factory.md`](research_factory.md). `quant.selection_bias` now computes DSR and
CSCV/PBO when a complete aligned trial matrix exists. `quant.research_factory` validates
pre-registered experiment bundles every post-close and applies chronological OOS, locked-holdout,
cost-stress, drawdown, DSR, and PBO gates. Its strongest possible terminal is a category-D
governance-review candidate; it cannot change production behavior. Daily session capture, corpus
rollup, and the complete 81-variant grid worker now produce those bundles automatically once the
point-in-time lake and forward sample pass their gates.

The data lake can now derive versioned `raw`, `split_only`, and `total_return` bar artifacts through
[`scripts/adjustment_policy.py`](../scripts/adjustment_policy.py), with cutoff-aware action
revisions, per-bar lineage, and independent rebuild verification. See
[`docs/adjustment_policy.md`](adjustment_policy.md). These artifacts may be
`quant_compatible`, meaning only that their shape passes the offline feature validator. The research
factory does not choose among arbitrary derived views. The preregistered grid worker now
deterministically materializes the required SPY `total_return` view from the current validated
snapshot and blocks when its source/provider/action lineage is incomplete. Any other quant-input
change remains an explicit, reviewed experiment configuration change.

[`scripts/quant/labels.py`](../scripts/quant/labels.py) now supplies the missing standalone
forward-label boundary between immutable prior-close features and later outcome research. It
binds every feature row back to the exact ordered symbol/SPY pre-event bar prefixes, uses the
versioned market calendar for non-compressed 1/3/5/10-session horizons, ignores observations after
an explicit as-of date, and distinguishes pending, incomplete, and excluded outcomes. Generic
archives remain arithmetic diagnostics; only a validated `total_return` adjusted view can mark a
complete label economically comparable. The artifact is unscheduled and feeds neither the factory
nor production. See [`docs/quant_labels.md`](quant_labels.md).

The offline lab also has a manifest-bound cost and portfolio layer:
[`scripts/quant/cost_model.py`](../scripts/quant/cost_model.py) rejects future/missing/capacity-
violating microstructure inputs, while
[`scripts/quant/portfolio_backtest.py`](../scripts/quant/portfolio_backtest.py) applies overlapping
cash, gross/net, position, sector, catalyst, and aggregate stop-risk constraints to the complete
source scenario denominator. Capital is normalized, same-session exits do not fund entries, and no
share count or account data is used. See [`docs/portfolio_backtest.md`](portfolio_backtest.md).
The scheduled grid worker now preregisters a complete portfolio plan for every variant and builds
the selected candidate only after a fresh locked-holdout backtest. Missing spread data remains null
and causes the cost gate to decline allocation; it is not imputed. The resulting portfolio is a
comparison artifact, not a new factory evidence gate or production signal.

[`scripts/quant/inference_audit.py`](../scripts/quant/inference_audit.py) can then audit portfolio
daily returns using Newey-West HAC, deterministic moving blocks, positive-autocorrelation
effective-N, and group-aware interval purge/embargo. It keeps SPA explicitly unavailable and does
not change current DSR/PBO or promotion gates. See
[`docs/quant_inference_audit.md`](quant_inference_audit.md).

The separate [`scripts/quant/spa_audit.py`](../scripts/quant/spa_audit.py) implements Hansen's
studentized stationary-bootstrap SPA test over a complete declared family of exactly aligned,
cost-compatible portfolio paths and one explicit benchmark. It reports lower/consistent/upper
p-values plus block-length sensitivity, but is not scheduled and feeds no factory or promotion
gate. See [`docs/quant_spa_audit.md`](quant_spa_audit.md).

[`scripts/quant/strategy_uniqueness.py`](../scripts/quant/strategy_uniqueness.py) adds the next
offline filter: it tests whether a hash-valid candidate portfolio is statistically distinct from
the incumbent strategy library and contributes incremental net-of-modeled-cost utility after
benchmark/incumbent spanning. Return correlation, active-day overlap, HAC residual alpha, and an
equal-weight incremental portfolio diagnostic must all clear the versioned policy. Its strongest
label is review-only; it is not scheduled, does not feed the factory, and cannot establish
profitability. See [`docs/strategy_uniqueness.md`](strategy_uniqueness.md).

[`scripts/quant/strategy_registry.py`](../scripts/quant/strategy_registry.py) and
[`scripts/quant/strategy_cohort.py`](../scripts/quant/strategy_cohort.py) freeze the comparison
denominator before that audit. The append-only registry is replayed through the candidate's
registration sequence; exactly one benchmark and every then-current incumbent are selected,
source/cost/date contracts are revalidated, and the cohort hash is bound to the nested uniqueness
result. The repository registry is genesis-only, so this is operational groundwork rather than
real performance evidence. See
[`docs/strategy_registry_and_cohorts.md`](strategy_registry_and_cohorts.md).

[`scripts/quant/strategy_artifacts.py`](../scripts/quant/strategy_artifacts.py) provides the
write-once handoff into that registry surface. It builds a cost-contract-matched SPY total-return
reference on the candidate's exact date denominator and stages a factory candidate only when a
portfolio/export contract and every input were manifest-fingerprinted before the holdout lock.
Legacy bundles are blocked rather than reconstructed after results are visible. The tool never
mutates the registry or evaluates profitability. See
[`docs/strategy_artifacts.md`](strategy_artifacts.md).

[`scripts/quant/strategy_artifact_stager.py`](../scripts/quant/strategy_artifact_stager.py) closes
the unattended preservation loop without closing the governance loop. After the independent
factory runs, it stages only the exact current-run bundle whose own experiment terminal passed all
factory evidence gates. Existing packages are rederived and verified rather than overwritten; an
older candidate cannot authorize today's rejected bundle. The registry remains untouched. See
[`docs/strategy_artifact_staging.md`](strategy_artifact_staging.md).

[`scripts/quant/strategy_evidence_gate.py`](../scripts/quant/strategy_evidence_gate.py) closes the
offline evidence-joining loop while preserving the governance boundary. It requires the exact
current-run factory survivor and revalidated package, all 13 factory gates, locked-cohort
uniqueness, positive dependence-aware HAC and block-bootstrap lower bounds, and candidate-driving
SPA robustness across configured block lengths. Its strongest output only permits human review of
a future shadow observation; no registry, production, Buy, broker, or execution authority is
added. See [`docs/strategy_evidence_gate.md`](strategy_evidence_gate.md).

[`scripts/quant/strategy_shadow_monitor.py`](../scripts/quant/strategy_shadow_monitor.py) now
implements the next prospective boundary. A human-approved future-only plan freezes the evidence
dossier, registry/cohort denominator, strategy and cost contracts, calendar, fixed checkpoints,
and alpha schedule. Complete observations can only enter through hash-bound standard portfolio
results; terminal returns are re-derived from normalized NAV, the SPY path is semantically rebuilt,
and every closed session must have either a complete or explicit unavailable event. The strongest
terminal remains human-review-only and never feeds production. See
[`docs/strategy_shadow_observation.md`](strategy_shadow_observation.md).

[`scripts/quant/strategy_shadow_forward.py`](../scripts/quant/strategy_shadow_forward.py) now
implements the previously missing caller-dated state generator beneath that boundary. It advances
normalized cash and positions one market session at a time, charges modeled costs once, uses
conservative stop-first and gap semantics, maintains a continuous SPY benchmark, distinguishes
verified zero-signal from unavailable data, recursively replays prior state to genesis, and binds
the candidate/benchmark/incumbent roster through a shared self-commitment. That commitment catches
later subset deletion but does not prove when the specs were created, validate the plan bytes or
human approval, or prevent a same-user full-spec rebuild. It remains standalone and unscheduled;
the existing monitor still requires an approval-bound replay-intent migration and trusted outer
clock. See [`docs/strategy_shadow_forward.md`](strategy_shadow_forward.md).

## 6. Layered upgrade plan (summary — full detail in the readiness report)

Canonical source:
[`reports/readiness/fable5_quant_upgrade_orchestrator_research_2026-07-09.md`](../reports/readiness/fable5_quant_upgrade_orchestrator_research_2026-07-09.md);
execution detail in
[`reports/readiness/fable5_sprint_implementation_plan_2026-07-08.md`](../reports/readiness/fable5_sprint_implementation_plan_2026-07-08.md).

| Layer | Theme | One-line goal | Status (2026-07-12) |
|---|---|---|---|
| 0 | Orchestrator invariants | Every run ends in one deterministic terminal state + exactly one terminal artifact | Implemented: supervisor, reconciliation, timeline, freeze-completion probe, and deterministic no-live terminal |
| 1 | Data authority + point-in-time lake | Typed adapters, immutable lake/snapshots, security master, vintage stamps | Phase 1A + WP-09 semantics implemented: SEC/BLS/local adapters, manifests, readiness, 05:00 runner, and cutoff-aware raw/split/total-return views; commercial corporate actions and broad licensed history remain missing |
| 2 | Catalyst intelligence | Pre-decision event ledger + catalyst graph + event taxonomy + deterministic verdicts instead of prose catalysts | Foundation implemented: tamper-evident pre-decision shadow ledger, exact symbol/claim joins, three-layer source identity, append-only corrections/as-of replay, taxonomy, source-origin dedupe, conflict graph, immutable diagnostics; graph migration and market-reaction/"already priced" modeling remain |
| 3 | Quant research lab | `scripts/quant/` event studies, point-in-time features/labels, walk-forward, conservative backtests, model diagnostics, outcome attribution (offline) | Implemented and diagnostic-only: immutable capture, exact calendar forward labels, 81-variant worker, DSR/PBO, holdout/cost stress, preregistered portfolio/export contracts, total-return benchmark binding, predecision cost, overlapping portfolio, HAC/block-bootstrap/effective-N, purge/embargo, uniqueness, locked comparison cohorts, strategy-artifact bridge/staging, cross-diagnostic evidence gate, future-only hash-chained shadow observation, and the standalone stateful forward engine; monitor migration and broad survivorship-aware real artifacts remain open/data-waiting |
| 4 | Scoring & risk upgrade | Companion diagnostics (explain/calibration/scenarios) beside the unchanged production scorer | Implemented: `score_explain.py`, `score_calibration.py`, `risk_scenarios.py`, `correlation_risk.py`; production scorer/risk gate unchanged |
| 5 | LLM ensemble upgrade | Stricter discovery/verification split + manual Fable/Opus review lanes (artifacts only) | Planned |
| 6 | Model risk governance | Model inventory, model-change journal, drift monitor | Implemented: validated model inventory, `journal/model_changes.md`, `model_monitor.py`, feature/label lineage, grid-worker lineage, cost/portfolio/inference/registry/cohort/uniqueness/artifact-bridge/stager/evidence-gate/shadow registrations, and research-factory selection-bias gates; no approved expected-performance baseline yet |
| 7 | Monitoring & postmortems | Desk-style health console + structured postmortems | Morning/timeline/postmortem tooling plus current-run post-close outcome, calibration, model-risk health, and append-only manual decision/fill observation contracts are operational; the observation corpus is still empty |

The report's highest-leverage conclusion stands: the bottleneck is **data authority and
orchestration reliability, not model intelligence** — more LLM tokens cannot convert stale or
limited data into an actionable Buy under this constitution.

## 7. Invariants that never change

Linked, not restated — these bound every layer above:

* Fail-closed everywhere; no silent pass ([`AGENTS.md §1.10`](../AGENTS.md)).
* Deterministic gates override all agent prose ([`AGENTS.md §4`](../AGENTS.md);
  `verify_prose_contract` in [`scripts/research_freeze.py`](../scripts/research_freeze.py)).
* Buy gate never weakened; Official Research never implies Buy ([`AGENTS.md §3b`](../AGENTS.md)).
* Execution permanently disabled; no order/broker-mutation surface exists
  ([`AGENTS.md §1`](../AGENTS.md), [`§11`](../AGENTS.md)).
* Ledgers and frozen reports are append-only history; never rewritten
  ([`AGENTS.md §3a`](../AGENTS.md), [`§12`](../AGENTS.md)).
