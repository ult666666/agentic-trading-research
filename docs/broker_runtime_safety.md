# Broker Runtime Safety SLI

Status: instrumentation foundation implemented 2026-07-10. Existing broker permission denies are
unchanged. Live order execution remains prohibited.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## Why Static Permission Validation Is Not Enough

[`validate_broker_permissions.py`](../scripts/validate_broker_permissions.py) proves that the known
tool registry and project settings classify 47 read tools and deny 25 preview/mutation tools. It
does not prove which tools a particular unattended model attempted to call.

The runtime invariant is:

```text
broker_preview_write_attempt_total == 0
```

No missing log, crashed wrapper, or absent hook may be interpreted as zero.

## Instrumentation Path

Every scheduled Claude lane now runs through
[`scripts/audited_claude.py`](../scripts/audited_claude.py), which loads the additional settings in
[`config/claude_broker_audit_settings.json`](../config/claude_broker_audit_settings.json).
The file adds hooks only; it contains no permission allow or deny entries and therefore does not
replace project settings.

Claude Code's documented hook lifecycle runs `PreToolUse` before the permission decision and
supports regex matching of MCP tool names. The audit matcher is `mcp__.*`:

- `SessionStart` writes the heartbeat required for observable coverage;
- known read-only broker calls are counted and allowed to continue to existing permissions;
- known preview/write calls are counted and immediately denied;
- unknown tools from broker namespaces and new write-like MCP tools are counted and denied;
- unrelated MCP tools are counted only for instrumentation coverage.

References: [Claude Code hooks](https://code.claude.com/docs/en/hooks),
[Claude Code permissions](https://code.claude.com/docs/en/permissions).

The wrapper rejects caller-supplied `--settings`, `--dangerously-skip-permissions`, and
`bypassPermissions`. On timeout or signal it kills the complete Claude child process group before
finalizing the receipt. It also kills residual descendants that remain in that original process
group after the direct child returns 0, before integrity comparison or an outer publisher can run.
The outer publisher lock tree and research-ledger lock file are part of the model-immutable control
plane, so an inherited sandbox cannot legitimately hold those transaction locks.

For canonical production roots, the wrapper also launches the model under a macOS Seatbelt
deny-write profile inherited by every descendant. Scripts, configs, prompts, schemas, LaunchAgent
definitions, project hooks, constitutions, manifests, the broker namespace, fractional capability,
model inventory, and the strategy registry are immutable for the whole child lifetime. This is the
security boundary for dynamically constructed Bash paths; text hooks remain earlier feedback. A
before/after in-memory inventory independently detects persistent byte, mode, symlink, or file-set
drift, restores the pre-child state, and quarantines same-child authority outputs. The Morning lane
additionally treats the complete reports tree and pre-model authority inputs as immutable and
allows successful-child deltas only in its explicit research/candidate-verification working set.

Morning publication is outside the model sandbox. A successful wrapper transcript must contain
exactly one wrapper-owned audit result; `model_integrity_binding.py` then revalidates its broker
receipt/event log and control-plane integrity receipt. The model's final
`research_freeze_request/2.0` binds the current run ID plus exact draft/evidence/candidates hashes.
`research_freeze.py` repeats both checks, commits them as publisher provenance, and active
consumers revalidate the durable receipt pair. Missing or invalid provenance cannot create even a
canonical failure receipt or occupy the day's terminal slot. A model rc=124 never adopts a receipt
because the outer publisher is called only after rc=0.

Known residual: process-group cleanup does not prove termination of a descendant that deliberately
calls `setsid()`. Such a process still inherits the deny-write sandbox, cannot write the publisher
or ledger lock namespaces, and any later model-artifact mutation revokes provenance on the next
read; nevertheless it can create availability/revalidation churn. Do not describe descendant
containment as complete or promote the coverage gate until a host-native detached-session test and
stable process-identity design (or an outer-owned immutable input seal) pass review.

The post-close narrative writer is single-attempt. Any nonzero model exit quarantines and restores
partial authority-bearing output before returning, so a later invocation cannot inherit it. Exit
`86` (broker violation), `87` (audit/coverage failure), and `88` (control-plane violation or failed
restoration) are explicitly non-retryable.

## Data Minimization

Artifacts live under:

```text
data/runtime/broker_audit/<target>/<run-id>/
  <invocation-id>.events.jsonl
  <invocation-id>.receipt.json
  <invocation-id>.integrity.json

data/runtime/broker_audit/<target>/sli.json
```

The event chain stores the tool name, classification, timestamps, policy hash, session-ID hash,
and canonical `tool_input` hash. It never stores raw tool input/output, account data, positions,
balances, credentials, or order payloads. Every line is sequence- and hash-chained; receipts and
daily SLI artifacts are self-hashed.

The autonomy health reconciler does not trust the daily SLI merely because its fields and hash
are internally valid. It re-derives the expected projection from the canonical receipts and their
event logs under the same target session, then requires every source hash, coverage value, count,
status, and safety flag to match. A coherently re-sealed SLI therefore cannot hide a recorded
preview/write attempt or manufacture verified-zero coverage.

## Status Semantics

- `zero_verified`: at least one valid receipt, every receipt has a SessionStart heartbeat, and
  preview/write plus unknown-broker attempt totals are zero;
- `unobservable`: a receipt exists but heartbeat/event integrity is incomplete;
- `not_observed`: no invocation receipt or orphan event log exists;
- `violation`: a preview/write or unknown-broker attempt occurred, a receipt is invalid, or an
  event log has no final receipt.

Observed preview/write or unknown-broker attempts always override a successful model exit with
exit `86`. Instrumentation setup failure returns `87`; control-plane drift, unavailable OS guard,
or incomplete restoration fails closed with `87` before launch or `88` after launch. Coverage-only
failure is currently shadow: the wrapper records `unobservable` but preserves the model exit code.
`--require-coverage` exists for later promotion but is not set by scheduled runners.

## Promotion Gate

Before coverage becomes a production fail-closed requirement:

1. inspect at least five natural runs across Morning, read-only Robinhood, post-open, and
   post-close lanes;
2. require a heartbeat and final receipt for every model invocation and timeout;
3. prove read-only broker calls remain usable and every mutation fixture is denied;
4. prove no child/descendant survives timeout or a normal direct-child exit;
5. review SLI surfacing and prepare an explicit change that adds `--require-coverage`.

`scripts/natural_run_acceptance.py` records whether each completed session satisfies this canary
evidence denominator and maintains a five-session rollup. Reaching five produces only
`ready_for_review`; it never adds `--require-coverage`, changes permissions, or promotes itself.

This safety layer never authorizes a broker call or changes a research conclusion.
