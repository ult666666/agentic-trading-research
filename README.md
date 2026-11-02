# Agentic Trading Research

[![Offline checks](https://github.com/ult666666/agentic-trading-research/actions/workflows/research-demo.yml/badge.svg?branch=main)](https://github.com/ult666666/agentic-trading-research/actions/workflows/research-demo.yml)

A public snapshot of a private system for researching swing trades of 1 to 10 trading days in
US-listed common stocks and unleveraged ETFs. Language models gather evidence and propose ideas.
Deterministic Python checks decide which ideas reach a person, and every claim must record its
source and when that source was read.

The system is for research only and makes no claim to be profitable. It cannot place, change or
cancel an order: broker access is read-only, and order and account-changing tools are
deny-listed. Nothing in this repository is investment advice.

Two examples written for this repository run on synthetic data, with Python 3.11 or newer and
only the standard library:

```bash
git clone https://github.com/ult666666/agentic-trading-research.git && cd agentic-trading-research
python3 -B examples/overfitting_demo.py         # backtest-overfitting diagnostics
python3 -B examples/fail_closed_gate.py --all   # the research review gate
```

## Question

Backtest 100 variants of a strategy on the same history, keep the best one, and its results will
look good even when none of the variants has any skill. How can a reviewer tell a lucky winner
from a skilled one?

## Result

[`examples/overfitting_demo.py`](examples/overfitting_demo.py) draws 100 fictional strategies
with no skill (Student-t returns, 1,260 periods) and keeps the one with the best Sharpe ratio. It
then repeats the experiment after giving strategy 0 a true edge of 3.0 in annualized Sharpe ratio.

| Measure (pass rule) | Lucky winner, no skill | Winner with an edge |
| --- | ---: | ---: |
| Annualized Sharpe ratio of the winner | 1.34 | 2.23 |
| PSR, ignoring the other 99 variants | 0.9989 | 1.0000 |
| DSR after 100 trials (0.95 or more) | 0.6928, flagged | 0.9870 |
| PBO (0.10 or less) | 0.3333, flagged | 0.0198 |
| Reality Check p-value (below 0.05) | 0.1038, flagged | 0.0020 |
| SPA p-value (below 0.05) | 0.1437, flagged | 0.0020 |

Judged on its own, the lucky winner looks almost certainly skilled. The four diagnostics that
account for the search all flag it, and all four pass the strategy with a real edge.

The effect is not specific to one seed. `--null 400` runs a smaller version of the experiment
in 400 synthetic universes with no skill anywhere. The winner's PSR still passed 0.95 in 64.7% of
them. A strategy judged alone passes about 5% of the time. The DSR, PBO and Reality Check
brought the winner's false-pass rate back to 5% or below. SPA rejected in 9.5% of runs with the
demo's bootstrap block length, for a reason that
[examples/README.md](examples/README.md#pass-rates-over-400-synthetic-universes) explains.

## Method

| Diagnostic | Question it answers | Source |
| --- | --- | --- |
| Probabilistic Sharpe Ratio (PSR) | How likely is the true Sharpe ratio above a benchmark, given the sample length, skewness and kurtosis? | [Bailey and López de Prado 2012](https://doi.org/10.21314/JOR.2012.255) |
| Deflated Sharpe Ratio (DSR) | The PSR against the Sharpe ratio the best of N no-skill trials would reach | [Bailey and López de Prado 2014](https://doi.org/10.3905/jpm.2014.40.5.094) |
| Probability of backtest overfitting (PBO) | How often does the in-sample winner land below the median out of sample, over every symmetric split (CSCV)? | [Bailey, Borwein, López de Prado and Zhu 2017](https://doi.org/10.21314/JCF.2016.322) |
| Reality Check and SPA test | Does the best strategy beat the benchmark once the search over all N is accounted for? | [White 2000](https://doi.org/10.1111/1468-0262.00152), [Hansen 2005](https://doi.org/10.1198/073500105000000063), stationary bootstrap of [Politis and Romano 1994](https://doi.org/10.1080/01621459.1994.10476870) |

Each diagnostic is a small module in
[`examples/backtest_diagnostics/`](examples/backtest_diagnostics/) that takes plain lists of
per-period returns. The tests check the DSR against the published example in Bailey and López de
Prado (2014), PBO against a brute-force implementation, and the error rates of all four by
simulation. [examples/README.md](examples/README.md#backtest-overfitting-diagnostics) gives the
formulas, the checks and the limits.

## Reproduce

```bash
python3 -B -m unittest discover -s examples -p 'test_*.py' -v   # tests for both examples
python3 -B examples/overfitting_demo.py                          # exits 1 if a verdict is unexpected
python3 -B examples/overfitting_demo.py --null 400               # pass rates when nothing has skill
python3 -B examples/overfitting_demo.py --psr 4000               # PSR for strategies judged alone
```

## The research review gate

The private system's central rule is that model output is an input to a deterministic gate,
never the gate itself. [`examples/fail_closed_gate.py`](examples/fail_closed_gate.py) shows that
rule on fixed synthetic inputs. One scenario returns `REVIEWABLE_RESEARCH`, which still needs a
person's review. The other five return `HOLD` with machine-readable reasons, such as a stale
quote, a missing second source, or persuasive model text that tries to override stale evidence.
[examples/README.md](examples/README.md#the-research-review-gate) lists the scenarios.

## What is in this snapshot

| Path | Contents |
| --- | --- |
| [`examples/`](examples/) | The review gate and the backtest-overfitting diagnostics, with their tests, written for this repository |
| [`scripts/lib/`](scripts/lib/) | Safety and integrity code: the risk gate, broker tool allowlists, receipt hardening, run locks, secure hashing and the credential loader |
| [`scripts/quant/`](scripts/quant/) | Offline research diagnostics: forward labels, walk-forward tests, selection-bias checks, and SPA and inference audits |
| [`scripts/`](scripts/) | Orchestration, health and audit modules |
| [`schemas/`](schemas/) | JSON Schemas for the artifacts the system writes |
| [`docs/`](docs/) | Design notes for individual components |
| [`tests/`](tests/) | Tests from the private repository |

The snapshot is for reading. Many modules import private modules that are not included, so they
do not run here, and the design notes in `docs/` describe the private system, so many files they
name are private. Configuration, market data, prompts, agent instructions, logs, reports and the
research journal are not included. Vendor market data is licensed for personal use and cannot be
redistributed.

## How the private system is built

1. Model output is an input to a deterministic gate, never the gate itself.
2. Missing, stale or malformed evidence stops the run instead of producing a confident report.
3. Every claim records its source and when that source was read, and stale claims are rejected.
4. Health monitoring runs outside the pipeline it watches.
5. A person makes every trading decision and places any order by hand.

## What CI checks

The [Offline checks workflow](.github/workflows/research-demo.yml) runs on every push and pull
request, on Python 3.11 and 3.13, with read-only repository permissions and no secrets:

1. the tests in `examples/`, which check the diagnostics against closed forms, a published
   example, brute-force references and null simulations;
2. the review gate's six scenarios, and the diagnostics demo, which fails unless every
   diagnostic flags the no-skill winner and passes the strategy with an edge;
3. the `tests/` suite, where tests that need private modules or files are skipped, and each skip
   names what is missing;
4. a compile check of every Python file;
5. [`tools/check_markdown_links.py`](tools/check_markdown_links.py), which fails if any relative
   link in a Markdown file is broken.

## About

The full system is private, and this repository shows part of it. It was built with AI coding
assistants (Claude Code and Codex), and its research pipeline calls language models at run time.
Five modules in `scripts/lib/` come from a Claude skill, and one of them was modified;
[PROVENANCE.md](PROVENANCE.md) lists them and their license.

**Author:** Chunyu (Charles) Dai, Carnegie Mellon University ·
[portfolio](https://ult666666.github.io/) · [linkedin.com/in/cdandrs](https://www.linkedin.com/in/cdandrs)
