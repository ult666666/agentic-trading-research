# Deterministic Morning DAG

Status: version 0.1 shadow controller implemented 2026-07-10. It is not the production Morning
authority and cannot freeze, classify, score, authorize, or execute anything. Production Morning
publication remains owned by the receipt/evidence/source-bound `research_freeze.py` path; new
canonical legacy-v1 Official and appendix writes are permanently disabled.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## Purpose

The 2026-07-10 natural run proved that a single model session can complete specialist artifacts
yet still miss the 06:15 PT service objective before synthesis and freeze. The DAG moves control
of absolute deadlines, dependencies, process groups, cancellation, validation, persistence, and
terminal cardinality into deterministic Python.

[`config/morning_dag.json`](../config/morning_dag.json) freezes the shadow contract:

```text
05:38-05:52 snapshot/readiness (existing deterministic owner)
05:52-05:55 discovery workers
05:55-06:04 evidence workers
06:04-06:07 adjudication worker
06:07-06:09 authority/risk (existing deterministic owner)
06:09-06:12:30 optional synthesis worker
06:12:30-06:14 freeze (existing deterministic owner)
06:14-06:14:30 canonical terminal
06:15       service objective
```

Version 0.1 executes only model-worker nodes in an explicitly attended shadow canary. External
deterministic nodes are represented in the plan/state but deliberately not invoked. This prevents
the new controller from bypassing or duplicating existing authority while its latency and failure
behavior are evaluated.

## Safety Contract

- `production_enabled=false`, `shadow_only=true`, `execution_enabled=false`;
- no MCP servers and therefore no broker namespace;
- workers receive only `Read`, `WebSearch`, and `WebFetch` as explicitly configured;
- workers cannot use `Write` or `Bash`; Python owns all artifact writes;
- no fallback model, retry, dynamic command, shell, freeze, production-ledger, candidate, or
  strategy path;
- Haiku handles bounded extraction/research; Sonnet handles shadow adjudication/synthesis;
- Fable/Opus and every strongest-model lane are excluded from the critical path;
- every subprocess starts in its own process group and is terminated at the earlier of its worker
  timeout or absolute stage deadline;
- dependency failure prevents downstream process creation;
- parsed output is schema/target/agent/status checked and rejected on secret-like keys/values;
- raw stdout/stderr are not persisted;
- plan, state, worker-control results, and terminal artifacts are hash-bound;
- the immutable terminal grants no Official status, Buy authority, metrics, or production output.

The launchd plist does not set the canary environment variable. The production runner contains a
disabled-by-default plan-only hook; even when explicitly enabled it does not pass
`--execute-models` and cannot bypass or alter the production receipt-bound publication path.

## Artifacts

```text
data/runtime/morning_dag_shadow/<target>/<run-id>/
  plan.json
  state.json
  workers/<worker>.json        # only an attended model shadow run
  terminal.json

data/runtime/predecision_ledger_shadow/<target>/events.jsonl
                                # terminal denominator + strictly validated evidence rows
```

Terminal states are `shadow_plan_only`, `shadow_complete`, `shadow_partial`,
`shadow_deadline_expired`, and `shadow_failed`. They are operational diagnostics, never trading or
performance classifications.

## Commands

Validate the shipped contract:

```bash
python3 scripts/morning_dag.py check
```

Generate a no-model plan:

```bash
python3 scripts/morning_dag.py plan --target YYYY-MM-DD
python3 scripts/morning_dag.py run-shadow --target YYYY-MM-DD --run-id dag-<safe-id>
```

An attended worker canary requires both controls and should only run inside the valid Morning
window after the no-model/fault suite is green:

```bash
TRADING_MORNING_DAG_CANARY=1 \
  python3 scripts/morning_dag.py run-shadow \
  --target YYYY-MM-DD --run-id dag-<safe-id> --execute-models
```

This command still writes only shadow namespaces and does not run authority, freeze, report, or
production-ledger stages. The deterministic control plane appends a non-authoritative terminal
denominator to the pre-decision shadow ledger; valid evidence rows require the existing evidence
schema plus matching shadow agent hashes.

## Verification

[`scripts/test_morning_dag.py`](../scripts/test_morning_dag.py) covers strict configuration,
virtual deadlines, tool/MCP/model restrictions, dependency blocking, malformed/secret output,
whole-process-group termination, immutable/idempotent terminal behavior, self-hash validation,
double opt-in, the isolated pre-decision terminal capture, and a complete nine-worker fixture DAG
with zero production artifacts. [`scripts/test_event_ledger.py`](../scripts/test_event_ledger.py)
covers its chain, correction, as-of, duplicate-vintage, and invalid-evidence behavior.

## Promotion Gate

Production replacement is not authorized. Before proposing it:

1. observe at least five attended worker canaries without production writes;
2. prove every stage terminates by its deadline and all descendants are cancelled;
3. compare output completeness and source quality against the current receipt-bound production
   artifacts and preserved historical legacy artifacts;
4. implement deterministic partial-evidence adjudication and deterministic report fallback;
5. bind authority/risk/freeze to the existing validators without copying their logic;
6. prove exactly one validated terminal by 06:15 PT under timeout, malformed output, source outage,
   and process-crash faults;
7. prepare exact protected-file patches and obtain explicit approval before changing
   `config/research_budget.yaml` or `prompts/morning_scan.md`.

Rollback of version 0.1 is removal of the shadow config/module/tests/docs and the disabled runner
hook. Existing shadow ledger rows remain audit records; no report, production ledger, risk,
source-authority, or broker migration is involved.
