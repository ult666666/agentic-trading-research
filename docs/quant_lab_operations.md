# Quant Lab Operations

Status: implemented 2026-07-09. `AGENTS.md` overrides this guide.

The quant lab is an offline research surface. Its artifacts are diagnostic-only,
`feeds_production=false`, excluded from official performance, and unable to activate a model,
change a threshold/risk gate, authorize a Buy, or create an order.

## End-to-end experiment

1. Prepare a `quant_event_snapshot/1.0` JSON file. Every event needs an ID, uppercase symbol,
   trading-session date, UTC observed-at time, and optional structured features. The snapshot
   cutoff must be pre-open; prose is not accepted as a feature.
2. Build prior-close features:

   ```bash
   python3 scripts/quant/features.py build \
     --bars data/replay_bars.json --events /path/events.json \
     --out /path/features.json
   ```

3. Prepare a `quant_backtest_plan/1.0` file bound to the feature dataset and row hashes. Use
   long common stock or unleveraged ETF scenarios only. A post-open trigger requires a
   timestamped artifact hash; limit-touch entry slippage must be zero because a buy limit may
   not be modeled above its limit.
4. Create the experiment manifest before running the simulation. Include features, bars, plans,
   and every trigger artifact as snapshots; `--trials` is the full number of attempted variants:

   ```bash
   python3 scripts/quant/experiment_manifest.py create --id EXPERIMENT_ID \
     --data-snapshot /path/features.json \
     --data-snapshot data/replay_bars.json \
     --data-snapshot /path/plans.json \
     --param-grid '{"entry_model":["next_open","limit_touch","post_open_validation"]}' \
     --trials 3
   ```

5. Run the conservative simulator and diagnostics:

   ```bash
   python3 scripts/quant/backtest.py run \
     --features /path/features.json --bars data/replay_bars.json \
     --plans /path/plans.json \
     --manifest data/runtime/experiments/EXPERIMENT_ID/manifest.json \
     --out /path/backtest.json

   python3 scripts/quant/model_diagnostics.py analyze \
     --backtest /path/backtest.json --out /path/diagnostics.json
   ```

Daily bars cannot prove fills or sequence. The simulator assumes stop first when stop and target
touch in one bar, models gap-through-stop, leaves incomplete windows pending, and labels every
limit/trigger fill unguaranteed. Sharpe and Sortino are per hypothetical trade, not annualized.
DSR and CSCV/PBO remain null until their sample and return-matrix requirements are met.

## Companion diagnostics

- `scripts/score_explain.py`: exposes unchanged scorer arithmetic and only supplied rationale.
- `scripts/score_calibration.py`: summarizes provenance-valid outcomes by score, catalyst,
  regime, liquidity, price, and evidence buckets; n below 30 stays exploratory.
- `scripts/risk_scenarios.py`: per-share stop/gap/halt/slippage/partial/no-fill stresses. It never
  recommends shares; totals require an externally approved share count and artifact hash.
- `scripts/correlation_risk.py`: prior-close candidate/holding/portfolio/benchmark correlation
  and beta from sanitized weights. Threshold flags are not vetoes.
- `scripts/model_monitor.py`: validates inventory and supplied diagnostic artifacts, checks
  feature staleness/availability and prior-run drift, and writes operator health artifacts.

The scheduled post-close runner generates a target-specific score calibration and model-risk
monitor after the provenance-bound outcome chain. A same-date stale calibration cannot pass a
new run because both source run ID and through-date are checked. Blocked monitors are retained
for review rather than hidden.

## Interpretation limits

The fixed replay corpus is narrow, daily, and lacks volume and point-in-time news. Alpha Vantage
EOD acquisition helps settle research outcomes but is not live-price authority or a broad
licensed historical lake. No diagnostic demonstrates that the system will make money. Production
activation still requires replay evidence, explicit approval, a governed change record, and a
post-activation review; risk limits and execution remain outside this lab.
