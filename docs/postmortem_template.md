# Postmortem Template — field guidance

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Generated postmortems live in `reports/postmortems/<target>_<trigger>.md` and are produced by
`scripts/postmortem.py` (never by hand-editing an existing postmortem — the generator never
overwrites; a follow-up gets a `-2` suffix). A postmortem is a **diagnostic** record for this
research-only system: it re-classifies no report, touches no ledger or receipt, claims no
performance, and contains **statuses only — never secrets, balances, positions, or account
identifiers** (the generator scrubs and allowlists everything it renders).

Triggers (one per document): `no_terminal_artifact` · `duplicate_terminal_artifact` · `timeout` ·
`downgrade_after_eligible` · `prose_contract_mismatch` · `stale_primary_streak` ·
`credit_exhaustion` · `other`.

Change-control discipline (dates, previous rule / new rule, evidence, expected effect, review
date, rollback condition) is defined once in **AGENTS.md §12 — Learning & versioning** and is
not duplicated here: a postmortem *feeds* that process, and any approved rule change it produces
is logged in `journal/system_changes.md` under §12's required fields.

## Header (auto-emitted)

Generation timestamp (UTC), target session date, trigger, and the standing diagnostic
disclaimer. Backfilled postmortems must carry an explicit label naming the backfill date and the
source files every claim is grounded in.

## Incident

One tight paragraph: what happened, when (PT + UTC where relevant), and how it terminated.
State the *observed* facts first; label anything inferred as inferred. No speculation here.

## Timeline

Ordered wall-clock entries ("HH:MM:SS PT — event"), each traceable to a log line, health
artifact, receipt, or commit. The generator appends the auto-collected run timeline
(`data/runtime/health/<target>_timeline.json`) when present; absence is noted, never fabricated.

## Impact

What the system did and did not produce: report/artifact outcomes, ledger effect (normally
"none"), metrics effect (`counts_in_metrics`), operator effect. Always state explicitly whether
any broker mutation occurred (for this system the true answer is always "none — execution
permanently disabled"; say it anyway, grounded in the run-status flags).

## Root cause(s)

Distinguish the *proximate* cause (what tripped) from the *systemic* cause (why the design let
it trip). Multiple causes are normal; number them. Ground each in a named source file or log
line; label inferences.

## What worked

Controls that held (fail-closed gates, downgrade banners, read-only broker surface, labeled
preliminaries). Recording these prevents "fixing" controls that were actually load-bearing.

## What failed

Controls that were missing, mis-scoped, or fail-open — including observability gaps ("we could
not tell X from Y"). One bullet per failure, matched against the root causes.

## Corrective actions

One bullet per action, each with: the change, the **commit** (short hash + subject), and the
**regression test** (`scripts/test_*.py`) that locks it. Actions that change strategy, scoring,
or risk rules additionally require a `journal/system_changes.md` entry per AGENTS.md §12.
Distinguish shipped actions from proposed ones.

## Lessons

Candidate process lessons only. Promotion path: record in `journal/lessons.md` (its template
requires process-vs-outcome separation and sample size / confidence), and only then — with
evidence — a rule change per AGENTS.md §12. Never change strategy on one outcome.

## Review date

When the corrective actions are re-examined for effectiveness (e.g. "after 5 more production
Level-1 runs"), consistent with the review dates recorded in `journal/system_changes.md`.

## Auto-collected context / Sources (auto-emitted)

Allowlisted, scrubbed key-value dumps of `<target>_run_status.json` and
`<target>_reconciliation.json`, then the list of evidence files/references supplied to the
generator. Every substantive claim in the sections above should be traceable to one of these
sources.
