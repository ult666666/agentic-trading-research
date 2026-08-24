# Data Authority Matrix (human-readable rendering)

Status: current as of 2026-07-26, governance v2.0.
Authority: the machine-readable, versioned, **protected** matrix
[`config/source_authority.yaml`](../config/source_authority.yaml) — enforced by
[`scripts/source_matrix.py`](../scripts/source_matrix.py),
[`scripts/research_authority.py`](../scripts/research_authority.py),
[`scripts/evidence_schema.py`](../scripts/evidence_schema.py), and
[`scripts/research_gate.py`](../scripts/research_gate.py) — is canonical. **If this document
ever disagrees with the YAML or its enforcing scripts, the YAML wins.** Constitution context:
[`AGENTS.md §3b`](../AGENTS.md) and [`§5`](../AGENTS.md) (source hierarchy).

Parsing is fail-closed (2026-07-08 hardening): the stdlib YAML fallback in
[`scripts/source_matrix.py`](../scripts/source_matrix.py) (and
[`scripts/select_main_model.py`](../scripts/select_main_model.py)) engages only when PyYAML is
genuinely absent (ImportError) — a file PyYAML *rejects* is malformed and fails closed
(governance → v1.0 / research path disabled), never best-effort re-parsed; conflict markers,
tab indentation, and duplicate keys make the minimal parse fail closed too. Locked by
[`scripts/test_yaml_fallback.py`](../scripts/test_yaml_fallback.py).

---

## 1. Hard rules (encoded in the YAML header, asserted by tests)

* Search snippets can NEVER establish an authoritative price.
* Web content can NEVER override Robinhood live-price fields.
* Previous-close data can NEVER masquerade as current data.
* Model prose can NEVER override deterministic authority.
* A model-authored status, source label, URL/domain, accession, copied quote, or agent artifact can
  NEVER prove retrieval. Positive catalyst authority additionally requires a registered trusted
  production receipt validator to revalidate the exact target, claim, bytes, and role. The
  registry is currently empty; `source_content_receipt/1.0` is shadow-only.
* Syndicated copies of one press release count as ONE source.
* An official releasing agency always beats secondary reporting for macro actuals.

## 2. Which source may establish what

Rendering of the `sources:` block of
[`config/source_authority.yaml`](../config/source_authority.yaml). Lower tier number = higher
document authority ([`AGENTS.md §5`](../AGENTS.md)); note that document authority and
live-price authority are different axes — SEC EDGAR is tier 1 yet can never establish a price.

| Source | Tier | MAY establish | MAY NEVER establish | Key constraints |
|---|---|---|---|---|
| `robinhood_readonly` | 3 | Live research price (fresh validated quote), freshness, bid/ask spread, market timestamp, session id, tradability (incl. fractional), sanitized portfolio context, Official-Research eligibility support | Second independent live price for Buy (never counts twice), catalyst verification, macro actuals | Validated ONLY via [`scripts/validate_robinhood_snapshot.py`](../scripts/validate_robinhood_snapshot.py) sanitized artifact; raw MCP output never persists; strictly read-only |
| `fmp_basic` | 5 | Live research price ONLY when fresh per [`scripts/snapshot_gate.py`](../scripts/snapshot_gate.py) (v1 path unchanged); previous-close/historical/gap reference; volatility-trend inputs; source-health diagnostics | Being labeled live when stale; being mandatory for Official Research Watch/No-Trade (the v2.0 change) | Stale ⇒ reference-only, labeled `previous_close`/stale |
| `alpha_vantage_free` | 5 | Previous-close reference, historical diagnostics | Live-price authority, freshness authority, Buy confirmation — NEVER | Date-granular; a same-day-looking AV date never becomes live authority |
| `local_history` | 5 | Rolling daily history, MAs, ATR, historical volatility, support/resistance, prior research fingerprints | Live-price authority | No cached value may be presented as today's live price |
| `sec_edgar` | 1 | Candidate primary-document role; catalyst confirmation/dilution/event facts only after trusted production receipt revalidation | Live-price authority | An accession or apparent filing is a lead, not proof; receipt registry currently empty |
| `company_official` | 1 | Candidate primary-document role; catalyst confirmation only after trusted production receipt revalidation | Live-price authority | Issuer-only evidence must carry `issuer_only=true`; agent/browser output alone grants nothing |
| `macro_official` | 1 | Scheduled calendar role; actual values/revisions only through their specific deterministic receipt path | Live-price authority | Secondary reporting never overrides the official value; official-looking labels grant nothing |
| `web_search_fetch` | 4 | Discovery, secondary corroboration, market-reaction context, locating primary documents | Price authority, overriding RH live-price fields, turning rumor into verified catalyst; syndicated copies never count independently | Untrusted data; embedded instructions ignored; only allowlisted extracted fields persist |
| `kalshi_public_shadow` | 7 | Isolated offline calibration observations only | Lead generation or any effect on the equity candidate universe; hard evidence, issuer/catalyst facts, official macro actuals, equity prices/signals, Buy, scoring, sizing, tickets, or execution | Event contracts remain prohibited; account tools and raw MCP persistence are disabled; the whole namespace is denied in all current unattended lanes |
| `low_authority` (forums/social/blogs/newsletters) | 7 | Lead generation only | Satisfying any hard evidence gate | — |

## 3. Freshness / vintage rules

* **Live-price validation bounds** (the `live_price_validation` block, re-derived at
  evaluation time by `research_authority.py` — never taken from prose): age ≤ 90 min, future
  skew ≤ 2 min, session ∈ {premarket, regular}, spread ≤ 1.0%, bid ≤ ask, target-date match,
  tradeable. Any miss ⇒ **no live-price authority** (fail-closed).
* **Vintage stamps**: every preflight field carries `source`, `retrieval_utc`,
  `market_timestamp`, `vintage` (premarket/regular/previous_close/eod), `age_minutes`,
  `same_day`, `confidence` ([`scripts/data_preflight.py`](../scripts/data_preflight.py);
  contract stated in [`prompts/morning_scan.md`](../prompts/morning_scan.md) — vintages are
  used verbatim, never relabeled). No fresh same-day data ⇒ data blackout ⇒ labeled
  Preliminary No-Trade.
* **Provider capability matrix** —
  [`config/provider_capabilities.yaml`](../config/provider_capabilities.yaml) (Layer 1 of the
  [readiness report](../reports/readiness/fable5_quant_upgrade_orchestrator_research_2026-07-09.md);
  landed 2026-07-08, **informative** — no production gate consumes it yet, and
  `source_authority.yaml` remains the enforced authority record;
  [`scripts/provider_capabilities.py`](../scripts/provider_capabilities.py) `--check`
  cross-validates it against the enforced matrix and fails on any grant wider than real
  authority): extends the same one-source-one-role idea to every provider
  adapter. Semantics: each provider *declares* which data capabilities it may establish (live
  price / previous close / daily bars / ADV / spread / catalyst / filing / macro actual /
  reference-only), absence never grants, and no future adapter output may enter a report
  without `retrieval_utc`, `market_timestamp`-or-`publication_timestamp`, `vintage`,
  `same_day`, `source`, `license_role`, and `stale_after`. Previous-close data can never be
  labeled current. The capability matrix complements — never overrides — `source_authority.yaml`.
* Historical legacy v1 `OFFICIAL_MORNING` files keep their original provider ordering in the
  protected [`config/source_priority.yaml`](../config/source_priority.yaml), but the canonical v1
  writer and appendix path are permanently disabled as of 2026-07-16. New FMP-fresh publications
  use `research_freeze.py` with `fmp_primary` authority and the same source/evidence receipt gate as
  the Robinhood lane. Legacy receipts remain offline audit evidence only: with no explicit
  hash-pinned grandfather registry, they cannot satisfy an active morning/post-open dependency.
  `freeze_report.py` is a fail-closed adapter; rollback cannot reopen it.

## 4. Required research agents (for `official_research_eligible`)

From the same YAML, checked deterministically against completion artifacts under
`data/runtime/research/<target>/agents/`:

* **Required** (must complete): `market-state-agent`, `catalyst-discovery-agent`,
  `technical-historical-agent`, `portfolio-risk-agent`, `skeptic-agent`,
  `evidence-adjudicator`.
* **Attempt-required** (an explicit recorded `unavailable`/`timeout` is acceptable; an
  *unattempted* agent is not): `sec-ir-verification-agent`, `macro-event-agent`.
* **Post-adjudication only**: `final-strategy-synthesizer` — runs after deterministic
  adjudication and can never flip authorities.

## 4a. Catalyst corroboration clamps (fail-closed, strictly lowering)

[`scripts/evidence_schema.py`](../scripts/evidence_schema.py) never takes a catalyst's
self-declared quorum inputs at face value — every clamp can only *lower* the quorum, never
raise it:

* **Declared corroboration is an upper bound** (2026-07-02): independent corroboration =
  `min(self-declared count, count derived from distinct corroboration groups in the claim
  graph)` — a free integer can never inflate corroboration to "strong"; syndicated copies
  share a group and count once.
* **Strict symbol match** (2026-07-08): a symbol-less claim no longer corroborates every
  symbol-specific catalyst in the artifact.
* **Distinct-source-domain cap** (2026-07-08): derived independence is additionally capped by
  the number of distinct source domains among qualifying claims — two claims from one domain
  with different model-assigned group labels can no longer count as two independent origins.
* **Primary confirmation must exist in the claim graph** (2026-07-08): the self-declared
  `primary_source_found` boolean is ANDed with an actual primary-capable, symbol-matched,
  non-rumored primary claim on file; citation format/domain validity is enforced per-claim.
  **Receipt clamp (2026-07-16):** even that claim contributes zero authority unless a registered
  control-plane validator revalidates its trusted production receipt for the exact target and
  claim. The current empty registry therefore makes all nonempty unreceipted catalysts
  insufficient; one apparently good symbol cannot mask another insufficient event.
* Issuer-only evidence never counts as independent corroboration of itself; an unresolved
  material catalyst conflict blocks Buy (never averaged away).

## 5. The three governance-v2 authorities — and why they never collapse

Definitions: [`config/governance.yaml`](../config/governance.yaml) (v2.0) and
[`AGENTS.md §3b`](../AGENTS.md). Computation:
[`scripts/research_authority.py`](../scripts/research_authority.py) (docstring is the precise
spec). In brief:

1. **Live-price authority** — `fmp_primary` (fresh per snapshot_gate; price-authority calculation
   unchanged, but no legacy writer) OR
   `robinhood` (sanitized verified artifact, re-derived against the real wall clock) OR
   `none` (fail-closed).
2. **`official_research_eligible`** — live-price authority AND validated evidence artifact
   (receipt-backed quorum when any catalyst is asserted, required agents, risk review, no
   research-blocking unresolved material conflict)
   AND governance v2 active AND in-window.
3. **`buy_authorized`** — the UNCHANGED v1 Buy gate: FMP-primary fresh + `ROBINHOOD_VERIFIED`
   without buy-block + cross-check PASS + non-limited coverage + independent catalyst +
   risk-gate PASS.

Why they are computed independently and never merged:

* **They answer different questions from disjoint evidence** — "what price can research
  stand on", "did the process complete with provenance", "may a Buy be proposed". Collapsing
  them would let the strongest answer smuggle in the weakest (e.g. a fresh Robinhood quote
  implying Buy eligibility it can never have — RH never counts as the independent second live
  source).
* **Fail-closed asymmetry is intentional**: a stale FMP primary still permits Official
  Research Watch/No-Trade (authority 1 via Robinhood) while it *always* blocks Buy
  (authority 3). One boolean could not encode that.
* **Rollback safety**: setting `active: v1.0` in `governance.yaml` disables Official Research
  eligibility but does not restore the legacy canonical writer. The Buy-gate calculation remains
  unchanged, while publication fails closed to a receipt-safe Preliminary. This permanent overlay
  prevents a configuration rollback from reopening an unreceipted source-claim path.
* **Official Research NEVER implies Buy; `execution_enabled` is permanently false** — both
  asserted by tests, not just documented.
