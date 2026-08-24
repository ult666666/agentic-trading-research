# Agent Context Index

## Purpose

`scripts/agent_context.py` compiles a bounded architecture map from authoritative local contracts.
It prevents routine agents from spending context on a line-by-line repository scan before they know
which component owns the question.

The compiler does not replace source reading. It changes the default read order from "load
everything" to progressive disclosure:

1. orient with the compact brief;
2. locate the owning domain and component in the index;
3. load one component contract;
4. read only the cited authoritative sources needed for the task.

## Context Levels

| Level | Command | Intended use | Hard character budget |
|---|---|---|---:|
| Brief | `python3 scripts/agent_context.py brief --format markdown` | Mandate, stage, safety, domains, read order | 6,000 |
| Index | `python3 scripts/agent_context.py index --format json` | Component discovery and routing | 36,000 |
| Component | `python3 scripts/agent_context.py show --component model.risk_gate --format markdown` | One bounded contract before targeted source reads | 9,000 |

The artifact reports an approximate token count using
`ceil(Unicode characters / 3)`. This is a conservative planning estimate, not a model-specific
tokenizer result. Agents must use the measured character budget as the deterministic limit.

## Sources and Freshness

The compiler reads only:

- `config/agent_context.json` for the domain map, architecture anchors, routing, budgets, and exact
  safety contract;
- `AGENTS.md` as a content hash, preserving the constitution as the higher authority;
- validated `data/model_inventory.json` entries for model purpose, status, tests, uses, and limits;
- validated `config/autonomy_workflows.json` entries for workflow schedules, dependencies, runners,
  and terminal contracts.

Each output carries SHA-256 hashes for all four inputs and an artifact hash. The output is rebuilt
from source on each CLI invocation and each operator-console cache refresh. It is not a mutable
knowledge base and does not ingest agent prose.

Adding or removing a model inventory entry requires an exact `model_domains` route. Unknown domains,
duplicate component IDs, missing/symlinked source contracts, invalid workflow dependencies, policy
drift, oversized output, hash drift, private-data shapes, or authority lies fail closed.

## Web Projection

The loopback console exposes the same bounded index at `GET /api/v1/context`. The System Map view
provides domain filtering, component search, source-contract references, validation status, and a
clipboard copy of the compact brief.

The endpoint has no component/file query and no arbitrary read path. Deeper component detail remains
a local CLI operation. This keeps the web response bounded and prevents the UI from becoming a
general repository or private-artifact browser.

## Exclusions

The index intentionally excludes report bodies and paths, prompts, runtime research artifacts,
symbols, prices, positions, balances, account identifiers, credentials, raw broker data, model
responses, and current research conclusions. It invokes no network, model, broker, or subprocess.

This is a documentation, navigation, and observability component only. It cannot authorize a Buy,
change strategy or risk limits, establish profitability, affect metrics, invoke a workflow, or
place, preview, simulate, modify, replace, or cancel an order.

## Validation

```bash
python3 scripts/agent_context.py check
python3 scripts/test_agent_context.py
python3 scripts/test_operator_console.py
python3 scripts/model_inventory.py validate
python3 scripts/documentation_truth.py check
python3 scripts/build_manifest.py --check
```

The adversarial suite covers schema and policy drift, exact inventory routing, source hashes,
deterministic compilation, progressive-disclosure budgets, unknown components, private-data shapes,
authority lies, excluded path classes, component detail, CLI behavior, and static absence of network,
model, broker, process, or order surfaces.
