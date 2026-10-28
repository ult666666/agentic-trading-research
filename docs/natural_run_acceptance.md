# Natural-Run Acceptance

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

`scripts/natural_run_acceptance.py` turns the already-validated end-of-day control-plane artifacts
into one bounded daily acceptance receipt and a multi-session canary rollup. It does not rerun a
workflow or call a provider.

## Independent conclusions

- **Observation eligible:** every latest workflow attempt is a terminal production attempt, never
  a dry-run or rehearsal.
- **Operational acceptance:** the market calendar, 14:15 PT terminal boundary, workflow outcomes,
  all five service SLOs, scheduler, wake, broker runtime, runtime-source binding, pre-session
  readiness, freeze integrity, the lossless candidate bundle, the sealed post-close terminal
  receipt, and research-only safety checks pass.
- **Research-output acceptance:** the freeze is Official Research and its evidence freshness
  evaluation ran without invalid claims, and its exact `candidates.json` bundle passes all
  receipt, artifact, and cross-binding validators with `dropped_count=0`. Buy and a trade are not
  required; an Official No-Trade with zero candidates is a valid accepted research output.
- **Broker-audit canary eligible:** operational acceptance passes, the runtime SLI is
  `zero_verified`, at least one read-only broker attempt was observed, and complete Morning,
  post-open, and post-close instrumentation receipts exist.

A valid Preliminary freeze may receive operational acceptance while research-output acceptance is
false. A missing or hash-mismatched freeze cannot receive operational acceptance, even if a health
artifact claims the Morning SLO was met.

## Bound evidence planes (receipt v1.1)

The observer reads `data/runtime/research/<target>/candidates.json` and validates it with
`freeze_candidates.validate_receipt_for_candidates`, `validate_artifact`, and
`validate_against_receipt`. The canonical file hash, bounded counts, source, Official/Buy flags,
and report hash are copied into the acceptance evidence. A zero-candidate bundle is valid; any
dropped candidate is not.

It also takes `state.workflows.post_close.latest_run_id`, resolves only
`data/runtime/post_close_runs/<target>/<run-id>/terminal_receipt.json`, and calls
`post_close_finality.validate_terminal`. The acceptance receipt binds the terminal file hash,
self-hash, cutoff hash, report hash, and exact run/state/outcome projection. A cutoff envelope by
itself is explicitly nonterminal.

Missing, malformed, tampered, stale, or wrongly bound evidence in either plane produces
`observed_not_accepted`. It can never produce operational acceptance or a canary-eligible session.

The receipt self-hash is only a corruption check. Before publication and again during every
rollup, v1.1 receipts are fully re-derived from the strict, non-symlink canonical health, autonomy,
freeze, broker-SLI, readiness, candidate, and terminal files. The broker SLI is itself re-derived
from its exact per-invocation receipts and chained event logs; a self-consistent zero-violation SLI
without those matching sources fails the broker check. A coherently rewritten receipt and
companion hash therefore cannot count unless the canonical source set reproduces the complete
receipt. Source validation is mandatory for v1.1 rollup eligibility; calling the structural
validator alone never establishes canary provenance.

Release evidence has two deliberately separate conclusions. The exact release-snapshot comparison
remains forensic: after legitimate generated outputs it may report `status=drift` and
`release_reproducible=false`. Operational acceptance instead consumes the validated
`runtime_binding_status`, which passes only as `match` or `expected_output_drift`. The latter
requires the runtime-provenance validator to bind every difference to an approved create-once or
append-only output and its required receipt while protected control source remains unchanged.

The bounded acceptance projection records the effective binding status and snapshot ID; the exact
health-artifact hash binds the complete forensic status, drift inventory, and reproducibility
result. Thus expected output drift stays visible without becoming a false natural-run failure.
Unapproved paths, staged changes, modified/missing control source, rewrites, deletions, invalid
receipts, and every unvalidated drift remain fail-closed and cannot receive operational acceptance
or canary credit.

## Daily automation

The existing 14:16 PT watchdog run writes:

```text
data/runtime/natural_run_acceptance/<target>.json
data/runtime/natural_run_acceptance/current.json
```

Earlier watchdog runs do not evaluate the day. Observer failure is fail-soft for the watchdog and
never changes a workflow result; it is logged and surfaced locally.

Only the exact two canonical output paths above are accepted; `--out` cannot redirect either
artifact. The dated receipt is published once with an exclusive create operation. Identical bytes
are idempotent, but different bytes can never replace the dated history. A `pending` observation is
returned to the caller without occupying that final slot. Operational acceptance and canary
eligibility also require generation on the target session's PT date at or after the configured
post-close boundary; a later replay/backfill is explicitly non-canary.
The canonical project root refuses the CLI `--now` override entirely; injected clocks remain
available only with an isolated test root and cannot publish into production history.

## Five-session rollup

The rollup requires five valid canary-eligible session receipts before it can become
`ready_for_review`. Even then:

- `promotion_authorized=false`;
- `production_gate_enforced=false`;
- a human governance review and a separate approved change are required;
- the count is not profitability evidence.

Tampered, duplicate-target, symlinked, duplicate-key JSON, wrongly named, or source-divergent
receipts block the rollup instead of being ignored. A dated filename must be exactly
`<target_session_date>.json`. Receipt inputs are represented only by hashes and bounded statuses.
No symbols, prices, report text, account data, credentials, model output, or broker payload is
copied.

Hash-valid `natural_run_acceptance/1.0` receipts remain auditable legacy records and are never
rewritten by the v1.1 evaluator. They do not count toward the v1.1 canary denominator; only v1.1
receipts with both new evidence checks and mandatory canonical-source reproduction passing can be
canary eligible. Legacy receipts are validated against their original v1.0 envelope; they are not
synthetically upgraded or judged by later workflow fields.
