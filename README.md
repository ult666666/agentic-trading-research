# Agentic Trading Research System

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org)
[![Scope](https://img.shields.io/badge/scope-research--only-informational)]()
[![Execution](https://img.shields.io/badge/order_placement-manual_only-critical)]()
[![Tests](https://img.shields.io/badge/automated_tests-3%2C300%2B-brightgreen)]()

A research-only operating system for catalyst-driven, 1–10 trading-day swing-trade
research on US-listed common stocks and unleveraged ETFs.

**It does not preview, simulate, place, modify, or cancel orders.** A human reads the
report and places every accepted order manually. That boundary is enforced in code, not
by convention.

> No component promises profit. A valid target is reproducible positive net-of-cost
> out-of-sample evidence with bounded downside; that evidence does not exist yet.
> That sentence is in the private repository's README too — the system is built to make
> its own failure legible rather than to look good.

---

## What this is, in engineering terms

An orchestration layer where **language models propose and deterministic code disposes.**
Model agents collect evidence, extract claims, and argue against each other's theses.
Everything that can actually change an outcome — freshness, coverage, risk, position
sizing, the freeze window, the terminal decision — is a deterministic gate written in
ordinary Python, with a test.

Scale, in the private working repository:

| | |
|---|---|
| Python modules | 365 in the main tree |
| Automated tests | 3,300+ across 292 test modules |
| Commits | 200+ |
| Scheduled daily stages | 20, driven by the market calendar rather than weekday arithmetic |
| JSON schemas | 123 |
| Architecture documents | 70 |

## The parts worth reading

**`AGENTS.md` / `CLAUDE.md`** — the binding authority documents. Every agent operates
under an explicit constitution: what it may assert, what evidence it must cite, which
gates it may never bypass, and the standing prohibition on storing credentials, account
numbers, or PII. Most of the engineering effort in this project went here rather than
into strategy.

**`scripts/lib/`** — the safety and integrity layer:

- `risk_gate.py`, `options_risk_gate.py`, `portfolio_concentration.py` — deterministic
  sizing and exposure limits
- `artifact_provenance.py`, `source_content_receipt`-backed flows — every claim traces to
  a receipt, and a stale receipt fails the run rather than degrading quietly
- `receipt_hardening.py`, `fence_replay.py` — tamper-evidence and replay containment
- `launchd_contract.py` — the scheduler contract, so a job that did not run is an error
  instead of silence
- `journal_event.py` — append-only journals
- `morning_report_console.py` — includes the private-key and credential **redaction
  regexes** applied to anything rendered to a console
- `secrets_loader.py` — the single sanctioned credential path, with a strict variable
  allowlist so a poisoned dotenv cannot introduce new names

**`scripts/quant/`** — the diagnostic research layer: labelling, inference auditing, and
selection-bias checks (`quant_spa_audit`, `quant_inference_audit`) used to attack the
system's own results.

**`schemas/`** — 123 JSON schemas. Artifacts are validated at boundaries, so a malformed
upstream response fails loudly at ingestion rather than silently three stages later.

**`docs/`** — 70 architecture and runbook documents, including
`documentation_truth.md`, which defines a mechanism for keeping the documentation
honest: generated sections of the docs are regenerated from the system's real
configuration, so a doc that drifts from behaviour is a build failure.

## Design principles this project actually enforces

1. **Deterministic where it matters.** Model output is an input to a gate, never the gate.
2. **Fail closed.** Manifest drift, an unsealed snapshot, or a stale receipt stops the
   day. A degraded run that still produces a confident report is the outcome most worth
   preventing.
3. **Provenance or it did not happen.** Claims carry receipts; receipts carry freshness.
4. **Independent watchdog.** Health and SLO monitoring runs outside the pipeline it
   watches, because a pipeline cannot be trusted to report its own silence.
5. **The human keeps the decision.** No order path exists in the codebase.

## About this repository

This is a **curated public snapshot** of a private working system, published to show the
engineering rather than to be run. It contains architecture documents, schemas, the
safety and integrity layer, the quant diagnostic layer, and a representative set of
orchestration modules and tests.

Deliberately **not** included: market data, cached vendor responses, configuration, the
research journal, logs, reports, and the complete strategy surface. Vendor market data
is licensed for personal internal use and is not mine to redistribute, and the runtime
state has no business being public.

Nothing here is investment advice.

**Author:** Chunyu (Charles) Dai — Carnegie Mellon, Artificial Intelligence + Business ·
[linkedin.com/in/cdandrs](https://www.linkedin.com/in/cdandrs)
