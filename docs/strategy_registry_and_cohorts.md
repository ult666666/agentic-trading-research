# Strategy Registry and Comparison Cohorts

Status: WP-21 implemented offline; registry is genesis-only and contains zero strategies.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. This layer controls research
denominators only. It does not activate a strategy, establish profitability, feed the research
factory or production path, change a score/risk/Buy/freeze gate, access a broker or account, or
create an order.

## Why This Exists

[`scripts/quant/strategy_uniqueness.py`](../scripts/quant/strategy_uniqueness.py) can test whether
a candidate adds net-of-modeled-cost information beyond a benchmark and incumbent library. A
loose invocation could still cherry-pick that comparison by omitting an inconvenient incumbent,
switching benchmarks, changing a source file, choosing a thin date intersection, or comparing
different cost contracts.

WP-21 closes that governance gap with two deterministic components:

1. [`scripts/quant/strategy_registry.py`](../scripts/quant/strategy_registry.py) maintains an
   append-only, hash-chained record of benchmark, incumbent, candidate-review, and retired
   references.
2. [`scripts/quant/strategy_cohort.py`](../scripts/quant/strategy_cohort.py) replays that registry
   exactly through a candidate's registration event, includes every incumbent reference known at
   that sequence, requires exactly one benchmark, verifies every frozen source binding, and only
   then hands the cohort to WP-20.

The registry begins at [`data/quant_strategy_registry.jsonl`](../data/quant_strategy_registry.jsonl)
with one genesis event. It currently contains no strategy records because no mature real
candidate/benchmark/incumbent portfolio panel exists.

## Registry Contract

Every event stores:

- monotonic sequence and UTC event time;
- previous-event SHA-256 and whole-event SHA-256;
- exact registry-policy SHA-256;
- fixed non-authority and no-execution fields; and
- one strict payload for genesis, strategy registration, or role transition.

A registration permanently binds a stable strategy ID to:

- role at registration;
- hypothesis and feature-family IDs;
- repository-relative allowlisted source path;
- portfolio result and canonical-content hashes;
- evaluation window;
- cost-stress multiplier;
- cost and portfolio policy versions/hashes; and
- a bounded evidence reference.

Source rebinding, duplicate registration, absolute paths, path traversal, internal symlinks,
future evaluation windows, malformed portfolio artifacts, and stale concurrent writers fail
closed. Readers take a shared file lock; appenders take an exclusive lock, validate the current
head, append exactly one canonical JSON line, flush, and `fsync`.

The allowed transitions are intentionally narrow:

- `candidate_review -> incumbent_reference` only with the category-D approval reason;
- `candidate_review -> retired_reference`;
- `incumbent_reference -> retired_reference`; and
- `benchmark_reference -> retired_reference`.

No retired reference can reactivate. A transition event requires a non-empty approval reference,
but that string is documentary evidence, not cryptographic proof of a human identity. The normal
change journal and explicit-approval process remain authoritative.

## Candidate-Sequence Cohort Lock

The candidate's original `candidate_review` registration sequence is the lock. The cohort builder
replays only the prefix ending at that event:

- exactly one `benchmark_reference` must exist;
- every `incumbent_reference` is included with no discretionary shortlist;
- references retired before the lock are disclosed and excluded;
- other candidates are disclosed but are not silently treated as incumbents; and
- registrations or retirements after the lock cannot rewrite that candidate's denominator.

The cohort stores the event-prefix count, prefix hash, lock-head hash, every selection decision,
every selected source binding, both policy hashes, and a whole-cohort hash. Appending future
registry events does not invalidate an old cohort; changing any event at or before its lock does.

## Comparability Preflight

Before a cohort can be `ready`, the builder requires:

- every selected source remains byte-semantically bound to its registration metadata;
- matching cost-stress multiplier, cost-policy version/hash, and portfolio-policy version/hash;
- a non-empty common-date intersection without imputation; and
- at least 95% common-date coverage for every source, inherited from the WP-20 policy.

It records aligned and candidate-active counts but does not compute return, Sharpe, alpha,
drawdown, expected-shortfall, or profitability metrics. A duplicate benchmark, empty incumbent
library, source drift, mismatched contract, or thin cohort produces a valid `blocked` artifact.
Blocked is a research terminal, not an error to bypass.

## Bound Audit Artifact

A `ready` cohort can produce `quant_strategy_cohort_audit/1.0`. The wrapper contains the exact
cohort hash, registry-lock head, nested WP-20 result, nested-result hash, classification, and its
own hash. Validation rechecks the complete nested uniqueness result. Before creating the wrapper,
the command semantically rebuilds the cohort from the current append-only registry prefix and
source files.

The strongest possible nested label remains `incremental_alpha_review_candidate`. It grants only
permission to assemble category-D review evidence. Locked holdout, DSR, PBO, real point-in-time
data, compatible costs, explicit approval, and a separate production patch remain mandatory.

## Commands

Check the current registry without writing:

```bash
python3 scripts/quant/strategy_registry.py check
```

Register a real frozen portfolio artifact only after its upstream evidence is ready:

```bash
python3 scripts/quant/strategy_registry.py register \
  --strategy-id candidate-v1 \
  --role candidate_review \
  --hypothesis-family catalyst-continuation \
  --feature-family prior-close-v1 \
  --source-path data/runtime/quant/strategy_artifacts/candidate-v1.json \
  --evidence-reference factory-bundle-sha256 \
  --occurred-at 2026-07-11T23:00:00Z
```

Build and verify the locked comparison cohort:

```bash
python3 scripts/quant/strategy_cohort.py build \
  --candidate-id candidate-v1 \
  --generated-at 2026-07-11T23:05:00Z \
  --out /tmp/candidate-v1-cohort.json

python3 scripts/quant/strategy_cohort.py verify \
  --cohort /tmp/candidate-v1-cohort.json
```

Run the bound WP-20 audit and validate the wrapper:

```bash
python3 scripts/quant/strategy_cohort.py audit \
  --cohort /tmp/candidate-v1-cohort.json \
  --out /tmp/candidate-v1-uniqueness-audit.json

python3 scripts/quant/strategy_cohort.py validate-audit \
  --artifact /tmp/candidate-v1-uniqueness-audit.json
```

These commands are offline and local. Registration and transitions are governance mutations and
must not be run speculatively merely to make a cohort pass.

## Honest Limitations

- A hash chain detects modification relative to its anchored head; it cannot prove that an
  operator registered every strategy that should economically exist. Process discipline and the
  critical integrity manifest remain necessary.
- Role and evidence-reference strings are structured metadata, not independent verification of
  economic validity or human approval.
- The cohort ensures comparable inputs; it does not correct selection bias, prove causal
  independence, or establish future returns.
- All source portfolio paths remain hypothetical diagnostics. The live registry is empty, so no
  real audit has been run through this path.
