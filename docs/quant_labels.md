# Point-in-Time Forward Labels

Status: implemented 2026-07-12 as a standalone, unscheduled offline diagnostic.

## Purpose

[`scripts/quant/labels.py`](../scripts/quant/labels.py) closes the gap between immutable
prior-close feature rows and later outcome research. It adds exact 1, 3, 5, and 10-session labels
without modifying the feature dataset or allowing future bars into feature construction.

This is research infrastructure, not a signal. The output cannot feed production scoring, the
Buy/risk/freeze gates, the research factory, strategy promotion, official metrics, a broker, or an
account. It makes no profitability claim.

## Inputs

1. A hash-valid `quant_feature_dataset/1.0` from
   [`scripts/quant/features.py`](../scripts/quant/features.py).
2. Either a validated generic daily OHLC archive or a hash-valid `adjusted_bar_view/1.0` whose view
   is `total_return` and whose bars pass the quant feature validator.
3. An explicit `--as-of` date, the versioned
   [`config/market_schedule.yaml`](../config/market_schedule.yaml), and
   [`config/quant_label_policy.json`](../config/quant_label_policy.json).

The tool recomputes every symbol and SPY ordered bar prefix with `date < event_date` and requires
those hashes and cutoffs to match each feature row. A different historical prefix is a hard
refusal, even if an outer dataset hash was recomputed.

## Label Contract

The event date is session one. The tool generates one exact ten-session spine from the versioned
market calendar, including holidays. For each requested horizon:

- previous-close return = horizon close / previous-session close - 1;
- event-open return = horizon close / event-session open - 1;
- excess return = symbol return - SPY return using the same baseline and exit;
- MFE = maximum daily high / event-session open - 1;
- MAE = minimum daily low / event-session open - 1.

Daily bars cannot determine intraday path order or establish a fill. MFE and MAE are descriptive
daily-bar bounds only.

Horizon states are explicit:

- `complete`: the exit is observable and every exact symbol/SPY session and baseline exists;
- `pending`: the horizon exit is after the requested as-of date;
- `incomplete`: an observable symbol/SPY session or baseline is missing;
- `excluded`: the source feature row was not eligible for modeling.

Missing sessions are never compressed or shifted. Calendar years absent from the versioned market
schedule fail closed.

## Adjustment Authority

A generic archive can support arithmetic diagnostics, but every result remains
`economic_return_eligible=false`. Only an independently hash-valid `total_return` adjusted view can
mark a complete label economically comparable. A raw or split-only adjusted view, a stale adjusted
view, or a tampered artifact is rejected rather than relabeled.

Bars after `--as-of` are excluded before OHLC validation, lineage binding, session checks, and
label math. Their values cannot affect label rows or the bounded source hash. The optional whole-file
hash can still identify that a different input container was supplied, without making its later
observations available to calculations.

## Commands

```bash
python3 scripts/quant/labels.py check

python3 scripts/quant/labels.py build \
  --features /path/to/features.json \
  --bars /path/to/bars-or-total-return-view.json \
  --as-of YYYY-MM-DD \
  --out /path/to/write-once-labels.json

python3 scripts/quant/labels.py validate --artifact /path/to/labels.json

python3 scripts/quant/labels.py verify \
  --artifact /path/to/labels.json \
  --features /path/to/features.json \
  --bars /path/to/bars-or-total-return-view.json \
  --as-of YYYY-MM-DD
```

`build` is atomic and write-once. `verify` validates the artifact, rebuilds it from the exact
inputs, and requires byte-equivalent structured content and the same dataset hash.

## Validation

```bash
python3 scripts/test_quant_labels.py
python3 scripts/test_quant_features.py
python3 scripts/test_adjustment_policy.py
python3 scripts/model_inventory.py validate
python3 scripts/agent_context.py check
python3 scripts/build_manifest.py --check
```

The dedicated suite covers hand-calculated returns, exchange-calendar horizons, Memorial Day,
pending labels, missing-session refusal, future-bar poisoning, feature-prefix mutation, generic
versus total-return authority, adjusted-view tampering/staleness, row bounds, JSON schemas,
semantic/hash tampering, write-once output, exact rederivation, and static absence of network,
model, broker, subprocess, and order surfaces.

## Next Gate

Do not connect this artifact to fitting, backtests, the research factory, or strategy review until
a separate work package defines a leakage-safe join and validates a broad, point-in-time,
survivorship-aware total-return sample. That integration must preserve source, feature, label,
calendar, adjustment, and as-of hashes and must not change any production authority.
