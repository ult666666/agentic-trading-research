# Autonomous Quant Research Factory

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: implemented and connected to the unattended 13:15 PT post-close workflow on
2026-07-09. The current production state is `collecting_point_in_time_data`; this is an honest
data-readiness terminal, not a strategy failure.

Authority: `AGENTS.md` overrides this document. The factory is an offline research
and governance system. It cannot access a broker, activate a strategy, change production scoring
or risk gates, authorize a Buy, or enable execution. Every order remains a manual human action.

## Why This Exists

Backtest selection can make weak strategies look strong when many variants are tried. Ordinary
in-sample return, Sharpe, or a conventional t-statistic is not enough. The factory therefore makes
the experiment history and selection process first-class artifacts:

1. define the hypothesis and complete parameter grid before selection;
2. count every trial, including rejected and abandoned variants;
3. use point-in-time features and chronological walk-forward folds with an embargo;
4. lock a final holdout before variant selection;
5. model costs and re-run at least a 2x cost stress;
6. require Deflated Sharpe Ratio and CSCV/PBO from a complete aligned return matrix;
7. stop at governance review even when every diagnostic passes.

Primary research informing these controls:

- Bailey and Lopez de Prado, [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551).
- Bailey, Borwein, Lopez de Prado, and Zhu, [The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253).
- Harvey, Liu, and Zhu, [And the Cross-Section of Expected Returns](https://www.nber.org/papers/w20592).

## Components

- `config/research_factory.json`: immutable safety policy,
  pre-registered strategy program, exact 81-variant grid, portfolio-export policy, walk-forward
  policy, and evidence gates.
- [`scripts/quant/selection_bias.py`](../scripts/quant/selection_bias.py): aligned return-matrix
  builder, DSR, and CSCV/PBO calculator.
- [`scripts/quant/session_capture.py`](../scripts/quant/session_capture.py): immutable daily join
  of validated catalyst, instrument, and pre-event bar artifacts.
- [`scripts/quant/feature_corpus.py`](../scripts/quant/feature_corpus.py): fail-closed rollup of
  all immutable session packages into one forward corpus.
- [`scripts/quant/grid_worker.py`](../scripts/quant/grid_worker.py): complete pre-registered grid,
  walk-forward selection, locked holdout, cost stress, predecision portfolio construction, and
  bridge-verifiable bundle writer.
- [`scripts/quant/cost_model.py`](../scripts/quant/cost_model.py) and
  [`scripts/quant/portfolio_backtest.py`](../scripts/quant/portfolio_backtest.py): available offline
  predecision cost and overlapping-capital diagnostics. The worker now creates a complete
  manifest-bound portfolio plan for every variant before holdout evaluation and evaluates the
  selected variant afterward. Missing historical spread data is retained as `null`; the cost gate
  declines the affected allocation instead of imputing a favorable fill. See
  [`docs/portfolio_backtest.md`](portfolio_backtest.md).
- [`scripts/quant/inference_audit.py`](../scripts/quant/inference_audit.py): optional HAC,
  moving-block-bootstrap, effective-sample, and group purge/embargo diagnostics over portfolio
  outputs. It is not a promotion gate and explicitly does not implement SPA; see
  [`docs/quant_inference_audit.md`](quant_inference_audit.md).
- [`scripts/quant/spa_audit.py`](../scripts/quant/spa_audit.py): separate offline Hansen SPA
  multiple-comparison audit over a complete, aligned family of portfolio paths and one explicit
  benchmark. It is not scheduled or wired to factory gates; see
  [`docs/quant_spa_audit.md`](quant_spa_audit.md).
- [`scripts/quant/research_factory.py`](../scripts/quant/research_factory.py): current-run-bound
  state machine, bundle validator, evidence gate, and health-artifact writer.
- `scripts/run_post_close_review.sh`: unattended fail-soft
  invocation before the narrative model starts.
- `data/runtime/health/<target>_research_factory.json`: one replaceable current health artifact.
- `data/runtime/experiments/<bundle>/bundle.json`: immutable experiment bundle entry point.
- [`schemas/quant_research_factory_config.schema.json`](../schemas/quant_research_factory_config.schema.json),
  [`schemas/quant_research_bundle.schema.json`](../schemas/quant_research_bundle.schema.json), and
  [`schemas/quant_grid_worker.schema.json`](../schemas/quant_grid_worker.schema.json): independent
  Draft 2020-12 contracts for configuration, immutable bundles, and current-run worker health.

No component imports a broker, model, network, or dynamic-command surface.

The unattended post-close order is fixed:

```text
session capture -> corpus rebuild -> grid worker -> independent factory evaluation
  -> current-run artifact stager -> narrative
```

Each step is fail-soft for the narrative but fail-closed for research evidence. A later step
cannot infer or repair a missing earlier artifact.

## Pre-Registered Baseline

`catalyst_continuation_baseline_v1` tests one explicit research hypothesis: a primary-source-
confirmed catalyst with positive prior-close relative strength may continue after the event when
the open is not chased and downside is bounded by a frozen ATR invalidation.

The grid is fixed before evidence selection:

- max entry gap: `0.25 / 0.50 / 0.75 ATR`;
- stop: `1.0 / 1.5 / 2.0 ATR`;
- target: `1.5 / 2.0 / 3.0 R`;
- maximum hold: `3 / 5 / 10 sessions`;
- total variants: `3 x 3 x 3 x 3 = 81`.

The program is long-only and limited to common stock and unleveraged ETFs. Rumors, squeeze-only
theses, social-only signals, future/revised data, raw LLM prose features, and prohibited
instruments are excluded.

Simulation assumptions are frozen with the grid: 10 bps entry slippage, 10 bps exit slippage,
25 bps stop slippage, and 10 bps base transaction cost per side. The selected final variant is
re-run at 25 and 50 bps transaction cost per side. These are modeled inputs, not observed fills.
The portfolio cost estimator uses only timestamped spread, prior-close annualized volatility, and
a pre-registered fraction-of-ADV assumption. The latter is a research assumption, not measured
participation. Missing spread or volatility remains `null`; it is never inferred from the scalar
backtest assumptions, and a missing required cost input prevents allocation.

## Forward Sample

Each valid post-close run attempts one immutable session package. Capture occurs only when the
data-lake receipt is `research_ready`, the catalyst graph is valid, the instrument was
point-in-time eligible, and the event was confirmed, non-binary, non-dilutive, non-stale,
non-rumored, and free of unresolved conflict. No eligible event is a valid zero-row terminal.

Every feature row stores hashes of the exact ordered symbol and SPY bars with
`bar_date < event_date`. Later outcome archives may append future bars; any historical change,
insertion, omission, or reordering fails the backtest binding. This permits real forward outcome
settlement without weakening look-ahead protection.

The corpus rollup validates every package, contained document, feature row, instrument type, and
hash. One damaged package blocks the entire corpus rather than being silently skipped. Below 30
eligible rows the corpus remains `accumulating`.

## Selection-Bias Contract

`quant_selection_matrix/1.0` is rows = settled observations and columns = variants. Every source
backtest must pass its own hash validation. Completed paths contribute modeled return after costs;
settled no-fill paths contribute zero. Pending, excluded, missing, duplicated, or non-aligned
observations fail closed.

`quant_selection_bias/1.0` is produced only with at least 30 observations and two variants. DSR
uses the declared trial count and selected-return skew/kurtosis. CSCV partitions observations into
even contiguous blocks and reports the fraction of in-sample winners that rank below the OOS
median. Missing variants make both diagnostics unavailable rather than optimistic.

Per-observation Sharpe is never annualized. The descriptive naive IID t-statistic is explicitly
non-authoritative because overlapping swing trades can be dependent.

## Experiment Bundle

`quant_research_bundle/1.0` binds, by exact path and SHA-256:

- the experiment manifest and complete pre-registered parameter grid;
- a verified walk-forward plan;
- each fold's conservative backtest and diagnostics;
- the aligned selection-bias artifact;
- a holdout locked before selection and chronologically after all walk-forward tests;
- one or more cost-stress backtests over the same observations; and
- for worker version `0.2.0` bundles, a pre-holdout export contract plus its resulting candidate
  portfolio and exact source backtest.

The factory independently revalidates every inner schema, source-result link, experiment ID,
parameter grid, trial count, date window, observation set, status, cost multiplier, and hash.
Absolute paths, `..` traversal, stale hashes, unregistered programs, and mismatched holdouts are
rejected. The export section is optional only for legacy bundle compatibility; legacy bundles may
still be evaluated by the factory but are intentionally ineligible for strategy-artifact export.

## Grid Worker

Once the corpus and lake are ready, the worker runs automatically without a model:

1. retain only eligible events with a fully settled maximum-hold outcome window;
2. materialize and validate a cutoff-aware SPY `total_return` view; missing coverage is a clean
   `data_not_ready` terminal;
3. lock the latest 252 calendar days as holdout, with a 10-day embargo before it;
4. build 504/126/126-day train/validate/test folds, a 10-day embargo, and 126-day step;
5. before any backtest, create every variant's full, holdout, stress, portfolio, and export-contract
   documents and fingerprint all of them in one experiment manifest;
6. run all 81 variants; trial count is variants, while scenario count is observations;
7. shortlist five variants by train Sharpe then mean return, select on validation, and test once;
8. select the final variant by fold-selection plurality, then mean fold-test return, then stable ID;
9. drop only the oldest pre-holdout remainder needed for eight equal CSCV slices;
10. compute DSR/PBO, run a fresh manifest-bound holdout backtest, build its normalized-capital
    portfolio, and re-run 2.5x/5x transaction costs;
11. preflight the complete bundle through `strategy_artifacts.py`, then atomically publish canonical
    JSON bytes so file hashes, object hashes, manifest hashes, and contract references are identical.

An all-zero/no-fill variant is assigned Sharpe 0 so a strict entry gate does not invalidate the
trial family. Constant nonzero return vectors remain invalid. CSCV ties use the pre-registered
lexicographically smallest variant ID and disclose the tie count.

## Evidence Gates

All gates must pass simultaneously:

| Gate | Current policy |
|---|---:|
| Completed walk-forward OOS observations | at least 100 |
| Locked holdout observations | at least 30 |
| Walk-forward folds | at least 5 |
| Positive-fold fraction | at least 60% |
| Worst fold expectancy | at least -2% |
| Holdout expectancy after modeled costs | greater than 0 |
| Holdout profit factor | at least 1.10 |
| Absolute holdout drawdown | at most 15% |
| Cost stress | at least 2x base cost and expectancy greater than 0 |
| Complete trial matrix | required |
| DSR probability | at least 95% |
| CSCV probability of backtest overfitting | at most 20% |
| Holdout locked before selection | required |

These are conservative governance thresholds, not promises. Passing produces only
`candidate_for_category_D_governance_review`. A separate reviewed patch, explicit approval,
system-change journal entry, shadow validation, and rollback plan remain mandatory before any
production rule can change.

## Uniqueness Audit Boundary

The separate [`quant.strategy_uniqueness`](../scripts/quant/strategy_uniqueness.py) diagnostic can
compare a successful candidate's net-of-modeled-cost portfolio path with an explicit benchmark and
the incumbent strategy library. It tests return correlation, active-day overlap, HAC residual
alpha after benchmark/incumbent spanning, and incremental equal-weight portfolio utility. See
[`docs/strategy_uniqueness.md`](strategy_uniqueness.md).

It is not part of this factory's evidence gates. The repository does not yet have a mature,
compatible candidate/benchmark/incumbent holdout panel, and the audit does not recompute DSR/PBO.
The separate [`strategy evidence gate`](strategy_evidence_gate.md) now binds a factory survivor to
the staged package, uniqueness, dependence-aware inference, and candidate-specific SPA. It does
not change this factory terminal, production strategy, Buy authority, risk gate, or execution
state.

The separate [`prospective strategy shadow monitor`](strategy_shadow_observation.md) begins only
after that evidence gate and explicit human approval. It freezes the exact cohort and strategy
contracts, rejects caller-supplied return scalars, requires one complete-or-unavailable event for
every closed market session, and evaluates only pre-registered prefixes. It remains outside this
factory and has no registration, promotion, production, Buy, broker, or execution authority.

The offline append-only registry and candidate-sequence cohort lock now provide the required
comparison-denominator contract before a uniqueness audit. They remain manual, genesis-only, and
outside this factory. The grid worker can now produce an export-eligible bundle, but no factory
candidate is staged globally, registered, promoted, or audited automatically.

## Daily State Machine

```text
invalid config/run/readiness -> blocked
valid but incomplete lake    -> collecting_point_in_time_data
ready lake, young corpus     -> session packages accumulate; grid worker waits
reviewable corpus            -> grid worker runs all pre-registered variants
ready lake, no valid bundle  -> awaiting_preregistered_experiments
bundle fails any gate        -> experiments_rejected_or_incomplete
all gates pass               -> candidate_for_category_D_governance_review
```

The post-close narrative displays only state, readiness blockers, program/trial counts, gate
failure codes, and next-action IDs. It does not display modeled returns or infer profitability.

## Current Blockers

The latest canary correctly remains data-waiting. Broad point-in-time daily bars, a symbol-interval
security master, real corporate-action and delisted coverage, historical spread/NBBO, and macro
publication timestamps are incomplete. Adjustment semantics are locally versioned and tested, but
no licensed action feed supplies broad coverage. The worker also requires a valid cutoff-aware SPY
total-return view; raw-only or incomplete corporate-action history cannot be substituted. Until
those inputs pass the existing data-lake and benchmark-view gates, the worker returns
`data_not_ready` without loading a model or fabricating a sample. Once the inputs and forward sample
mature, the scheduled chain progresses through bundle creation without an operator command.

## Operations

Run an attended read-only canary using a real post-close run ID:

```bash
RID="$(python3 -c "import sys; sys.path.insert(0,'scripts/lib'); import artifact_provenance as a; print(a.new_run_id('postclose'))")"
python3 scripts/quant/research_factory.py run \
  --target YYYY-MM-DD --run-id "$RID" \
  --readiness data/runtime/data_lake/readiness/YYYY-MM-DD.json \
  --experiments data/runtime/experiments \
  --out /tmp/research_factory.json --replace
```

Validate an artifact without changing it:

```bash
python3 scripts/quant/research_factory.py validate --path /tmp/research_factory.json
```

The scheduled path needs no operator command. `run_post_close_review.sh` invokes capture, corpus,
grid worker, and factory in order, retains blocked health artifacts when possible, logs each
result, invokes write-once staging only for the exact current bundle when the factory's strongest
terminal passes, and continues the narrative. Data-waiting and rejected states are normal
successful research terminals. See
[`docs/strategy_artifact_staging.md`](strategy_artifact_staging.md).
