# Agentic Trading Research

[![Offline checks](https://github.com/ult666666/agentic-trading-research/actions/workflows/research-demo.yml/badge.svg?branch=main)](https://github.com/ult666666/agentic-trading-research/actions/workflows/research-demo.yml)

A public snapshot of a private, research-only system for catalyst-driven swing-trade research
(1 to 10 trading days) on US-listed common stocks and unleveraged ETFs. Language models gather
evidence and propose ideas. Deterministic Python checks decide what passes, every claim must
carry a source receipt, and a person reviews the result.

There is no automated order execution. Broker access is read-only, order and account-changing
tools are deny-listed, and a person places any real order by hand. The system does not claim to
be profitable, and nothing here is investment advice.

## Run the synthetic example

[`examples/`](examples/) holds a small review gate written for this public repository. It uses
fixed synthetic inputs, so it needs no market data, API keys or network access, and it has no
order path. It needs Python 3.11 or newer and only the standard library.

```bash
git clone https://github.com/ult666666/agentic-trading-research.git && cd agentic-trading-research
python3 -B examples/fail_closed_gate.py --all
python3 -B -m unittest discover -s examples -p 'test_*.py' -v
```

One fictional scenario returns `REVIEWABLE_RESEARCH`, which still needs human review. Five
return `HOLD` with machine-readable reasons, such as a stale quote or a missing second source.
[examples/README.md](examples/README.md) explains each scenario and the limits of the example.

## What is in this snapshot

| Path | Contents |
| --- | --- |
| [`examples/`](examples/) | The runnable synthetic gate and its tests, written for this repository |
| [`scripts/lib/`](scripts/lib/) | Safety and integrity code: the risk gate, broker tool allowlists, receipt hardening, run locks, secure hashing and the credential loader |
| [`scripts/quant/`](scripts/quant/) | Offline research diagnostics: forward labels, walk-forward tests, selection-bias checks, and SPA and inference audits |
| [`scripts/`](scripts/) | Orchestration, health and audit modules |
| [`schemas/`](schemas/) | JSON Schemas for the artifacts the system writes |
| [`docs/`](docs/) | Design notes for individual components |
| [`tests/`](tests/) | Tests from the private repository |

The snapshot is for reading. Many modules import private modules that are not included, so they
do not run here. The docs describe the private system, and many files they name are private.
Configuration, market data, prompts, agent instructions, logs, reports and the research journal
are not included. Vendor market data is licensed for personal use and cannot be redistributed.

## What CI checks

The [Offline checks workflow](.github/workflows/research-demo.yml) runs on every push and pull
request, on Python 3.11 and 3.13, with read-only repository permissions and no secrets:

1. the synthetic gate's unit tests and its six scenarios;
2. the `tests/` suite, where tests that import private modules or read private files are
   skipped and each skip names what is missing;
3. a compile check of every Python file;
4. [`tools/check_markdown_links.py`](tools/check_markdown_links.py), which fails if any relative
   link in a Markdown file is broken.

## Design rules the system enforces

1. Model output is an input to a deterministic gate, never the gate itself.
2. Missing, stale or malformed evidence stops the run instead of producing a confident report.
3. Every claim carries a source receipt, and every receipt carries its freshness.
4. Health monitoring runs outside the pipeline it watches.
5. A person makes every trading decision. No code path places, changes or cancels an order.

## About the full system

The full system is private, and this repository shows part of it. It was developed with AI
coding assistants (Claude Code and Codex), and its research pipeline calls language models at
run time. Five modules in `scripts/lib/` come from a Claude skill, and one of them was modified;
[PROVENANCE.md](PROVENANCE.md) lists them and their license.

**Author:** Chunyu (Charles) Dai, Carnegie Mellon University ·
[portfolio](https://ult666666.github.io/) · [linkedin.com/in/cdandrs](https://www.linkedin.com/in/cdandrs)
