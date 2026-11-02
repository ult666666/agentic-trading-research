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

## Backtest-overfitting diagnostics

Testing many strategy variants and keeping the best one inflates its backtest: by chance alone,
the best of 100 no-skill strategies usually looks good. Quant reviewers therefore ask how many
variants were tried and whether the winner survives out of sample.
[`examples/backtest_diagnostics/`](examples/backtest_diagnostics/) answers with four standard
diagnostics. It is runnable, tested and uses only the standard library:

| Diagnostic | Question it answers | Source |
| --- | --- | --- |
| Probabilistic Sharpe Ratio (PSR) | How likely is the true Sharpe ratio above a benchmark, given the sample length, skewness and kurtosis? | [Bailey and López de Prado 2012](https://doi.org/10.21314/JOR.2012.255) |
| Deflated Sharpe Ratio (DSR) | The PSR against the Sharpe ratio the best of N no-skill trials would reach | [Bailey and López de Prado 2014](https://doi.org/10.3905/jpm.2014.40.5.094) |
| Probability of backtest overfitting (PBO) | How often does the in-sample winner land below the median out of sample, over every symmetric split (CSCV)? | [Bailey, Borwein, López de Prado and Zhu 2017](https://doi.org/10.21314/JCF.2016.322) |
| Reality Check and SPA test | Does the best strategy beat the benchmark once the search over all N is accounted for? | [White 2000](https://doi.org/10.1111/1468-0262.00152), [Hansen 2005](https://doi.org/10.1198/073500105000000063), stationary bootstrap of [Politis and Romano 1994](https://doi.org/10.1080/01621459.1994.10476870) |

```bash
python3 -B examples/overfitting_demo.py              # the synthetic demo below
python3 -B examples/overfitting_demo.py --null 400   # pass rates over 400 no-skill universes
```

The demo draws 100 fictional strategies with no skill (Student-t returns, 1,260 periods). The
winner has an annualized Sharpe ratio of 1.34 and a PSR of 0.9989, which looks significant if
the 99 discarded variants are ignored. The diagnostics flag it: DSR 0.6928, PBO 0.3333, Reality
Check p = 0.1038, SPA p = 0.1437. When strategy 0 is given an injected edge (annualized Sharpe
3.0), it becomes the winner and passes all four: DSR 0.9870, PBO 0.0198, both p-values 0.0020.
The data are synthetic and the demo's seed is arbitrary; nothing here describes a real strategy
or shows that any strategy is profitable.

Across 400 synthetic no-skill universes (20 strategies, 500 periods), the winner's PSR passed
0.95 in 64.7% of runs, against 5.00% for a strategy judged alone (4,000 strategies). The DSR
passed none. The mean PBO was 0.506. The Reality Check rejected at 5% in 4.5% of runs. SPA
rejected in 9.5% with a mean bootstrap block of 10 and 6.0% with a block of 1: on independent
data a long block makes SPA's studentization noisy, so choose the block for the data's serial
dependence. [examples/README.md](examples/README.md#backtest-overfitting-diagnostics) gives the
method, the checks and the limits.

## What is in this snapshot

| Path | Contents |
| --- | --- |
| [`examples/`](examples/) | The runnable synthetic gate, the backtest-overfitting diagnostics and their tests, written for this repository |
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

1. the examples' unit tests: the synthetic gate, and the backtest-overfitting diagnostics
   checked against closed forms, a published example, brute-force references and null
   simulations;
2. the gate's six scenarios, and the diagnostics demo, which fails unless every diagnostic
   flags the no-skill winner and passes the injected skill;
3. the `tests/` suite, where tests that import private modules or read private files are
   skipped and each skip names what is missing;
4. a compile check of every Python file;
5. [`tools/check_markdown_links.py`](tools/check_markdown_links.py), which fails if any relative
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
