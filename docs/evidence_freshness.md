# Evidence Freshness Gate

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

`scripts/evidence_freshness.py` applies deterministic, category-specific time-to-live limits to
the normalized claims in governance-v2 `evidence.json`. The policy is
`config/evidence_freshness.json`; its strict schema is
`schemas/evidence_freshness_policy.schema.json`.

## Authority rules

- In production, freshness is evaluated with an explicit control-plane allowlist of claim IDs
  whose trusted production source receipts were revalidated for the exact target and claim.
  `validation_status=validated` is necessary but never sufficient. An unreceipted claim is
  classified non-authoritative even when its timestamp is recent.
- The evaluator has no permissive default: every caller, including tests and replays, must pass an
  explicit authority allowlist. Omitting it is a call error; an empty set safely grants nothing.
- The trusted production receipt-validator registry is currently empty. Therefore direct
  web/SEC/IR findings, accessions, domains, copied quotes, and agent artifacts remain leads rather
  than fresh facts. `source_content_receipt/1.0` is shadow-only and cannot enter the allowlist.
- `retrieval_timestamp_utc` is the point-in-time anchor. A source may describe an older or future
  event, but the evidence must have been re-checked recently enough for its category.
- A claim's model-authored `stale_after` can make it expire sooner. It cannot extend the policy
  limit; later declarations are clamped deterministically.
- A stale claim is excluded from source independence and catalyst quorum. A stale thesis-supporting
  claim blocks Buy. Official Watch or No-Trade research may still proceed when the remaining
  evidence contract is complete.
- A future retrieval timestamp, invalid timestamp order, missing timestamp on a present claim, or
  unknown claim category is a hard blocker for Official Research.
- Missing and unavailable claims remain explicit non-present states and do not become fresh facts.

The supported categories and TTLs are versioned in the policy. Market-state, price, and liquidity
claims have the shortest limits; fundamental and reference facts have longer limits. Live-price
authority remains independently controlled by `scripts/research_authority.py` and the existing FMP
and read-only broker validators.

## Safety

The evaluator reads local normalized claims only. It performs no network, model, broker, account,
order, or external communication operation. It can only remove authority. It cannot authorize Buy,
change a strategy or risk limit, count a metric, freeze a report, or enable execution.
