# Replay & Backtest Plan

Status: current as of 2026-07-11 (quant stack, cost/portfolio/inference diagnostics, and manual
execution observation boundary).
Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. This file states — honestly —
what retrospective evaluation capability exists today, the layered plan for building a real
one, and the hard rule that bounds all of it: **a backtest result never activates anything.**

---

## 1. Honest current state (do not overstate this anywhere)

What exists:

* **One replay corpus**: [`data/replay_bars.json`](../data/replay_bars.json) — real Alpha
  Vantage daily OHLC for **4 symbols** (SPY benchmark + AAPL/MSFT/NVDA candidates), **no
  volume**, no intraday bars, no point-in-time news, static since 2026-06-23 (no refresh path).
* **One replay engine**: [`scripts/replay_session.py`](../scripts/replay_session.py) —
  **5 completed sessions** (2026-05-18 → 2026-05-22), **3 candidate names**, strict
  no-look-ahead discipline, outputs to [`data/replay_ledger.json`](../data/replay_ledger.json)
  + `reports/replay/<date>.md` + [`reports/readiness/replay-summary.md`](../reports/readiness/replay-summary.md).
  Its own docstring says it correctly: it tests **system behavior, not profitability** —
  results are retrospective, confidence LOW (daily bars only; the intraday fill rule is
  adapted to open+slippage and diverges from the [`PHASE3_PROTOCOL.md`](../PHASE3_PROTOCOL.md)
  fill rule).
* **The offline quant lab**: `scripts/quant/` — including
  [`scripts/quant/event_study.py`](../scripts/quant/event_study.py),
  [`scripts/quant/features.py`](../scripts/quant/features.py),
  [`scripts/quant/walk_forward.py`](../scripts/quant/walk_forward.py),
  [`scripts/quant/backtest.py`](../scripts/quant/backtest.py),
  [`scripts/quant/cost_model.py`](../scripts/quant/cost_model.py),
  [`scripts/quant/portfolio_backtest.py`](../scripts/quant/portfolio_backtest.py),
  [`scripts/quant/model_diagnostics.py`](../scripts/quant/model_diagnostics.py), and experiment
  manifests. The lab is
  **offline and diagnostic-only** by construction: nothing in the morning-run pipeline imports
  or invokes it; every output (including refusals) embeds the `DIAGNOSTIC_FIELDS` exclusion
  block (mirroring `diagnostic_record` in
  [`scripts/decision_path.py`](../scripts/decision_path.py)) so no lab record can ever count
  toward win rate / expectancy / official replay outcomes; missing/malformed/insufficient
  data ⇒ `{"error": ...}` + non-zero exit, never a fabricated value. Because the archive
  carries no volume, `volume_shock` is emitted as `null` with reason
  `volume_unavailable_in_archive` — never invented.
  Feature rows use a strict prior-close clock, carry per-feature null reasons and source hashes,
  and are invariant to future-bar truncation/poisoning. Backtests require an experiment manifest,
  the exact bar payload used by features, frozen plan timestamps, and fingerprinted post-open
  trigger evidence. Daily-bar ambiguity is stop-first; no-fill, exclusions, incomplete windows,
  costs, and gap-through-stop are explicit states.
* Behavior-level rehearsal and contract simulation:
  [`scripts/rehearse_session.py`](../scripts/rehearse_session.py) (replay-only production-path
  rehearsal) and [`scripts/premarket_simulation.py`](../scripts/premarket_simulation.py)
  (synthetic preflight-contract matrix). These validate *gates*, not returns.

* **Research outcome diagnostics**: [`scripts/point_in_time_bars.py`](../scripts/point_in_time_bars.py),
  [`scripts/outcome_tracker.py`](../scripts/outcome_tracker.py), and
  [`scripts/research_attribution.py`](../scripts/research_attribution.py). These create a
  vintage-stamped daily-bar archive, derive +1/+3/+5/+10 day MFE/MAE/target/stop diagnostics,
  and summarize attribution by catalyst/regime/score/data-quality buckets. Defaults write under
  `data/runtime/` and do not rewrite `data/shadow_ledger.json` or `data/research_ledger.json`.
  Output is explicitly labeled **research diagnostic, not executed trading performance**.
* **Report candidate extraction**: [`scripts/report_candidate_extractor.py`](../scripts/report_candidate_extractor.py)
  deterministically extracts Watch/Reject/Avoid/Buy-Proposal candidates from known morning-report
  sections when the governance-v2 research ledger points only at a report artifact. It reads
  symbols only from bold list headers in `Best Actionable Setups`, `Watchlist`, and
  `Avoid or Reject`; no-trade reports produce zero candidates; price/risk fields stay unknown
  unless written as explicit field-style values. Extracted candidates remain diagnostic-only and
  `counts_in_metrics=false`.
* **Manual execution observation boundary**:
  [`scripts/manual_execution_observation.py`](../scripts/manual_execution_observation.py) creates a
  complete case registry from every frozen candidate, then accepts append-only human attestations
  or sanitized read-only receipt anchors for decision/order-entry/fill/terminal events. It computes
  entry VWAP, fill ratio, timing, and proposal-relative shortfall without broker calls. No real
  corpus exists yet; human records remain unverified, receipt anchors remain unconfirmed, and
  effective spread/realized return stay unavailable.

What does **not** exist (state this plainly whenever replay evidence is discussed):

* No licensed broad same-day historical lake. The outcome archive can acquire Alpha Vantage EOD
  bars with vintage/provenance records, but that free source is rate-limited, narrow, and never
  live-price authority; missing provider data remains structured missing data.
* No intraday bars, no executed slippage, and no fill model matching actual human order
  placement. Daily high/low stop/target touches are conservative diagnostics and can be
  ambiguous when both levels touch in the same daily window.
* No reviewable outcome sample, intraday trigger corpus, populated human fill/slippage corpus,
  approved expected-performance baseline, Deflated Sharpe result, or CSCV/PBO return matrix. The
  observation contract now exists, but an empty schema is not evidence. The diagnostic engines
  refuse those claims and keep sample-size warnings mandatory.

Consequence (fail-closed posture): until enough completed diagnostic outcomes exist (the
attribution layer labels `n < 30` as not statistically reliable), no claim about strategy
performance is supportable. Every current replay/lab/outcome number is a labeled diagnostic
about *research behavior*, not proof of live trading performance.

## 2. The layered plan (build order, each layer usable on its own)

1. **Point-in-time archive** (operational for EOD outcome bars; broader lake still planned; Layer 1 of the
   [readiness report](../reports/readiness/fable5_quant_upgrade_orchestrator_research_2026-07-09.md)):
   `scripts/point_in_time_bars.py` stores append-only daily-bar vintages with provider,
   retrieval timestamp, source vintage, adjusted flag, validation status, and missing reason.
   The default archive root is `data/runtime/point_in_time_bars/`; a fuller `data/lake/`
   provider-backed implementation remains planned.
2. **Event studies + feature rows** (implemented): abnormal returns vs SPY over fixed trading-day windows,
   ATR-normalized moves, gaps — [`scripts/quant/event_study.py`](../scripts/quant/event_study.py),
   extended to real catalyst dates once the archive holds event history.
3. **Walk-forward structure** (implemented): time-ordered train/validate/test splits (never random),
   rolling windows, embargo around events, revised macro actuals only if stored point-in-time.
   Dev sample must stop equaling reported sample.
4. **Cost and portfolio scenarios** (implemented and factory-integrated, offline diagnostics): manual-compatible fill models only (next open, limit-touch,
   post-open trigger; **no fill guarantee**), explicit slippage/transaction-cost scenarios,
   gap-through-stop modeling, no-fill/exclusion tracking, cutoff-aware half-spread/square-root-
   impact estimates, and overlapping normalized-capital constraints. The scheduled grid worker now
   freezes every variant's complete portfolio plan and export contract before holdout, then builds
   the selected candidate afterward. Historical spread/NBBO is still absent; missing inputs remain
   null and prevent allocation rather than being imputed.
   A current-run controller stages that candidate and a rederived SPY reference only when the
   independent factory gives the exact same bundle its strongest review-candidate terminal.
   Staging is write-once and does not register or activate the strategy.
5. **Outcome attribution, manifests, and diagnostics** (implemented; DSR/PBO deferred until the sample supports it):
   `scripts/outcome_tracker.py` and `scripts/research_attribution.py` compute diagnostics over
   completed research outcomes while separating preliminary, watch-only, and official-eligible
   records. `scripts/report_candidate_extractor.py` expands report-level governance-v2 ledger
   rows into machine-readable diagnostic candidates without fabricating symbols from free prose.
   Every experiment
   records data-snapshot hashes, code version, parameter grid, and **trial counts** (without
   trial counts, Deflated-Sharpe / probability-of-backtest-overfitting math is impossible
   later). DSR/PBO and confidence intervals activate only when sample size warrants —
   sample-size warnings are mandatory output until then.

## 3. The hard rule (never weakened)

**No backtest, replay, event study, or lab diagnostic activates a strategy, scoring, or
method change.** Activation requires the full category-D path
([`scripts/improvement_review.py`](../scripts/improvement_review.py) classification):
**replay evidence + explicit user approval + a
[`journal/system_changes.md`](../journal/system_changes.md) entry** with date / previous rule /
new rule / reason / evidence / expected effect / review date / rollback condition, per
[`AGENTS.md §12`](../AGENTS.md) ("do not change strategy on one outcome"). Category-E
(risk-rule) changes are never automated at all, and the protected
[`config/risk_limits.yaml`](../config/risk_limits.yaml) can only ever be tightened by callers
(locked by [`scripts/test_risk_gate_limits.py`](../scripts/test_risk_gate_limits.py)).

Corollaries:

* The production scorer ([`scripts/score_candidate.py`](../scripts/score_candidate.py)) and
  its thresholds stay frozen until replay evidence + approval exist; lab diagnostics run
  *beside* it, never inside it.
* Replay results are kept **separate** from the official shadow trial; only Official Morning
  Report candidates count toward Phase-3 stats ([`AGENTS.md §3a`](../AGENTS.md)).
* No lab output may claim or imply profit. Retrospective results do not prove live
  performance — say so in every artifact that shows one.
