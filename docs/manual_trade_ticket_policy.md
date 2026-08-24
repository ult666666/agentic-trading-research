# Manual Trade Ticket Policy

Status: current as of 2026-07-08 (Fable 5 upgrade sprint).
Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. The authoritative ticket
format lives in the **protected, read-only**
[`prompts/morning_scan.md`](../prompts/morning_scan.md) ("User Action Ticket" and "Trade Plan
format" sections) — this file explains the policy around it and never redefines it.

---

## 1. What a ticket is — and is not

A **User Action Ticket is a proposal, never an order.** The system's entire deliverable is a
*proposed manual order ticket* that a human may or may not choose to place in Robinhood
([`AGENTS.md §0`](../AGENTS.md), [`§9`](../AGENTS.md)). A ticket is never confirmation that an
order exists, and the system **never** claims an order was submitted, filled, modified, or
canceled unless a connected read-only brokerage tool explicitly returned that state in-session
([`AGENTS.md §1.2`](../AGENTS.md)). A broker review/preview call is not permission to submit —
and previews are deny-listed anyway (see §4).

## 2. Required fields (full list — a ticket missing any is not a ticket)

Per the User Action Ticket block of [`prompts/morning_scan.md`](../prompts/morning_scan.md)
(header: *"proposal only — NOT a confirmation an order exists"*):

1. **Ticker**
2. **Desired entry**
3. **Maximum entry** (max acceptable chase price)
4. **Stop** (invalidation price)
5. **Target 1**
6. **Target 2**
7. **Shares or sizing formula** (see §3)
8. **Order-type suggestion** — only assertions supported by the verified record
   [`data/fractional_capability.json`](../data/fractional_capability.json) via
   [`scripts/lib/fractional_policy.py`](../scripts/lib/fractional_policy.py); never
   hand-asserted order-type limits ([`AGENTS.md §3a`](../AGENTS.md))
9. **Setup expiration time** (when the proposal goes stale)
10. **Conditions under which the order MUST NOT be placed**

Each ticket is backed by a full Trade Plan (same prompt, "Trade Plan format"): classification,
conviction, composite score, current **verified** price + timestamp, catalyst + date/time +
primary source, bull/bear case, entry zone/trigger, stop-loss + thesis invalidation, targets,
holding period, R:R, position size + maximum planned loss, liquidity/spread, portfolio
correlation, key risks, downgrade triggers, final action — with **verified facts / assumptions
/ speculation / unknown** kept separate ([`AGENTS.md §9`](../AGENTS.md)).

## 3. Sizing

The only sizing formula ([`AGENTS.md §8`](../AGENTS.md)):

```
shares = max_acceptable_dollar_loss / |entry − stop|     (always round DOWN)
```

Risk budget per trade and every limit come from the protected
[`config/risk_limits.yaml`](../config/risk_limits.yaml), enforced fail-closed by
[`scripts/lib/risk_gate.py`](../scripts/lib/risk_gate.py) — since the 2026-07-08 hardening the
protected file is the **limits authority**: caller-supplied limits may only tighten, and a
missing/unparseable limits file fails the gate closed
([`scripts/test_risk_gate_limits.py`](../scripts/test_risk_gate_limits.py)).

**Percentage-only fallback:** when portfolio equity is unavailable or unverified, the ticket
must NOT contain a share count. It gives percentage-based sizing + the formula + a clearly
labeled blank for the user's account value — and the risk gate fails closed on missing/
unverified account equity, so no gate-passed absolute size can exist in that state. A modeled
invalidation loss is **not guaranteed** (gaps, halts, slippage) — the ticket says so whenever
relevant.

## 4. Non-negotiables

* **Missing stop, target, or verified catalyst ⇒ not actionable.** A candidate cannot be an
  actionable Buy if current price, liquidity, or catalyst details cannot be verified —
  downgrade to Watch ([`AGENTS.md §1`](../AGENTS.md), [`§1.10`](../AGENTS.md): missing/stale/
  conflicting/malformed data ⇒ NO TRADE / WATCH, never a silent pass).
* **Execution is permanently disabled.** `execution_enabled: false` in
  [`config/governance.yaml`](../config/governance.yaml); every broker mutation/preview tool is
  deny-listed and validated before any unattended run
  ([`scripts/validate_broker_permissions.py`](../scripts/validate_broker_permissions.py));
  `executed_order_count` in [`data/research_ledger.json`](../data/research_ledger.json) is
  permanently 0. No ticket, score, or model output changes this.
* **The three authorities never collapse** ([`AGENTS.md §3b`](../AGENTS.md),
  [`docs/data_authority_matrix.md`](data_authority_matrix.md)): live-price authority,
  `official_research_eligible`, and `buy_authorized` are computed independently by
  [`scripts/research_authority.py`](../scripts/research_authority.py). **Official Research
  never implies Buy**; a fresh Robinhood quote never counts as the independent second live
  source; a stale FMP primary always blocks Buy. A ticket may only propose a Buy when
  `buy_authorized` is true — and even then it remains a proposal.
* A high score never auto-produces a ticket: risk-gate PASS + Risk-Manager approval are still
  required ([`AGENTS.md §7`](../AGENTS.md)), instruments outside long US-listed common stock /
  unleveraged ETFs are prohibited outright ([`AGENTS.md §1.5`](../AGENTS.md)), and no ticket
  ever guarantees or implies profit ([`AGENTS.md §1.6`](../AGENTS.md)).

## 5. Post-ticket observations

The observation-only boundary in
[`scripts/manual_execution_observation.py`](../scripts/manual_execution_observation.py) may later
record a human decision, human-entered order attestation, sanitized read-only order-history receipt,
partial fill, or terminal outcome. It starts from every frozen candidate, including no-action and
no-fill cases, and never edits the ticket or upgrades its authority. Human statements remain
unverified attestations; hash-valid read-only receipts remain receipt-anchored but unconfirmed by
the observer. See [`docs/manual_execution_observation.md`](manual_execution_observation.md).
