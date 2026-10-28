# Cost and Portfolio Backtest

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: the version 1.0 offline cost estimator and overlapping portfolio simulator are implemented.
They are diagnostic-only. The scheduled post-close grid worker now preregisters a complete plan
for every strategy variant and builds the selected holdout candidate after selection, but this
artifact does not change the research factory's evidence gates or any production workflow. Real
historical spread/NBBO and observed implementation-shortfall data are still missing.

Authority: `AGENTS.md` overrides this document. The system remains research-only;
orders are manual, broker write tools remain denied, and execution is permanently disabled.

## Components

- `config/quant_cost_policy.json`: cost formula, input limits,
  stress multipliers, no-fallback rule, and safety contract.
- [`scripts/quant/cost_model.py`](../scripts/quant/cost_model.py): cutoff-aware cost estimate and
  validation CLI.
- `config/quant_portfolio_policy.json`: normalized capital,
  gross/net, position, sector, catalyst, stop-risk, open-position, and daily-entry limits.
- [`scripts/quant/portfolio_backtest.py`](../scripts/quant/portfolio_backtest.py): complete-
  denominator validation, constrained allocation, daily mark-to-market, lineage, and result CLI.
- [`schemas/quant_portfolio_plan.schema.json`](../schemas/quant_portfolio_plan.schema.json):
  predecision allocation metadata contract.
- `scripts/test_quant_portfolio_backtest.py`:
  adversarial and file-level CLI tests.

## Cost Contract

For a predecision observation with spread `s`, daily volatility `v`, fraction of ADV `q`, impact
coefficient `k`, fixed fee `f`, and configured floor `m`:

```text
half_spread_bps = s / 2
impact_bps      = k * v * sqrt(q)
cost_bps_side   = max(m, f + half_spread_bps + impact_bps) * stress_multiplier
```

The shipped coefficient is 0.5, the floor is 5 bps per side, and allowed stress multipliers are
1x, 2x, and 3x. These are conservative research assumptions, not calibration to observed fills.

Every context carries both `observed_at_utc` and `decision_cutoff_utc`. An observation after the
cutoff is rejected. Missing spread, volatility, or ADV fraction produces `unestimable`; it never
becomes zero or a hidden default. Spread, participation, or volatility above a policy limit
produces `capacity_rejected`.

Fill probability is always `null`. Daily aggregates do not contain order-book queue position,
venue routing, or enough sequence information to justify a fill probability.

## Predecision Portfolio Plan

`quant_portfolio_plan/1.0` must contain every scenario from the original frozen
`quant_backtest_plan/1.0`, including paths that later become no-fill, excluded, or pending. Each
allocation fixes before outcome evaluation:

- scenario, symbol, and event date;
- deterministic priority and requested normalized weight;
- sector and catalyst type;
- plan timestamp;
- timestamped cost context and source-artifact hash.

The experiment manifest must fingerprint the bars, original backtest plan, portfolio plan, cost
policy, and portfolio policy. A success-only backtest subset cannot replace the original scenario
denominator.

For factory program version `0.2.0`, the worker creates these documents for every variant before
running any backtest. It also fingerprints the holdout corpus, full/holdout/cost-stress source
plans, SPY total-return view, and a pre-holdout export contract. The selected candidate is built
only from its fresh manifest-bound holdout backtest; plans are never reconstructed after results
are visible.

## Allocation and Accounting

Capital starts at normalized NAV 1.0. It is not account equity and no share count is emitted.
Completed hypothetical source paths are considered in ascending predeclared priority, then stable
scenario ID. Allocation is capped simultaneously by:

- available opening cash;
- 60% gross and net exposure;
- 12% per position;
- 30% per sector;
- 40% per catalyst type;
- 2.5% aggregate modeled stop risk;
- six open positions;
- three new positions per session;
- one open position per symbol.

The applied per-side cost is the greater of the source backtest's frozen cost and the predecision
cost estimate. Entry and exit costs reduce normalized cash. Positions are marked to the daily
close, except that a modeled source exit price is used on the exit date.

Daily bars cannot prove whether an exit happened before a different entry. The simulator therefore
does not reuse same-session exit proceeds for entries. A missing bar anywhere between a completed
path's entry and exit blocks the whole artifact.

Market moves can push an already-open position above an entry-time concentration limit. Those
post-entry breaches are reported in the daily path; they are not silently treated as allocation
errors or retroactively resized.

## Outputs

`quant_portfolio_backtest/1.0` contains:

- one decision for every original scenario and explicit non-allocation reasons;
- normalized daily NAV, return, drawdown, cash, gross/net exposure, sector/catalyst weights, and
  aggregate stop risk;
- turnover and modeled-cost attribution;
- source, policy, plan, manifest, decision-list, daily-path, and whole-result hashes;
- zero actual orders, zero account data, zero share recommendations, and zero production authority.

The result is hypothetical research performance. It is excluded from official win rate,
expectancy, and executed-trade metrics and cannot establish profitability.

Portfolio daily returns may be audited for serial dependence and leakage using
[`scripts/quant/inference_audit.py`](../scripts/quant/inference_audit.py). See
[`docs/quant_inference_audit.md`](quant_inference_audit.md). That audit is optional and does not
change any factory gate.

## Commands

Validate the policies:

```bash
python3 scripts/quant/cost_model.py check
python3 scripts/quant/portfolio_backtest.py check
```

Estimate a predecision context:

```bash
python3 scripts/quant/cost_model.py estimate \
  --context <cost-context.json> --stress 1 \
  --out <new-cost-estimate.json>
python3 scripts/quant/cost_model.py validate --artifact <new-cost-estimate.json>
```

Run a manifest-bound portfolio diagnostic:

```bash
python3 scripts/quant/portfolio_backtest.py run \
  --backtest <backtest.json> \
  --bars <bars.json> \
  --source-plans <backtest-plans.json> \
  --portfolio-plan <portfolio-plan.json> \
  --manifest <manifest.json> \
  --cost-policy config/quant_cost_policy.json \
  --portfolio-policy config/quant_portfolio_policy.json \
  --stress 1 \
  --out <new-portfolio-result.json>
python3 scripts/quant/portfolio_backtest.py validate \
  --artifact <new-portfolio-result.json>
```

Both CLIs refuse to overwrite an existing artifact.

## Current Honest State

The code now measures overlapping-capital and concentration effects correctly when complete,
predecision-bound inputs exist. The current data lake does not yet contain approved historical
NBBO/spread coverage, and no corpus of human entry, fill, partial-fill, cancellation, or exit
timestamps exists. Therefore the scheduled factory remains on its existing scalar cost stresses;
automatic portfolio-stage integration must wait for those inputs and a separate reviewed change.
