# Documentation Truth Contract

Static operator documentation is useful only when its claims match versioned machine contracts.
This control derives a small, non-private fact set from the repository and fails when the README,
operating state, or setup checklist drifts from it.

## Canonical Inputs

`scripts/documentation_truth.py` reads only local structured source files:

- autonomy workflow schedules and safety flags;
- service start/terminal objectives and watchdog checkpoints;
- repository LaunchAgent definitions;
- strict broker permission policy and namespace inventory;
- zero-dollar data-procurement policy;
- governance version and execution boundary;
- machine wake requirement; and
- the tracked portfolio fallback's absence of account values and identifiers.

It never inspects live broker/account state, invokes a connector, reads secrets, contacts a
network, starts a model, or changes a scheduler. Loaded/running status, provider reachability,
authentication, positions, balances, and current market data are deliberately excluded because
those facts are dynamic and belong in runtime health receipts.

## Checked Surfaces

- `README.md` contains an exact generated schedule block.
- `OPERATING_STATE.md` contains an exact generated operator-facts block.
- `MISSING_INPUTS.md` contains stable manual/optional prerequisites only.
- all three documents contain required command references and none of the known stale claims.
- tracked text files are scanned for masked account identifiers.
- `config/portfolio.yaml` must keep account value, buying power, and timestamp unknown/null; fresh
  read-only runtime data or percentage-only sizing is required instead.

The generated blocks are bounded by HTML comments and compared byte-for-byte. A config or broker
namespace change therefore requires the documentation to change in the same release.

## Commands

Validate without writing:

```bash
python3 scripts/documentation_truth.py check
```

Render the expected generated blocks without editing files:

```bash
python3 scripts/documentation_truth.py render --block all
```

Write an optional self-hashed local receipt:

```bash
python3 scripts/documentation_truth.py check \
  --out data/runtime/health/documentation_truth.json
```

Run focused tests:

```bash
python3 scripts/test_documentation_truth.py
```

## Authority Boundary

This is an operator-safety and release-quality check only. It cannot grant source, evidence,
live-price, scoring, risk, Buy, freeze, metrics, strategy, profitability, broker, order, or
execution authority. A healthy static receipt also does not prove that launchd ran, a provider is
reachable, or a trading workflow succeeded; use the runtime health commands named in
`OPERATING_STATE.md` for those questions.
