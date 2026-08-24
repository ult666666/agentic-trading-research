# Autonomous Shadow Operations (sim/research_qualification/shadow_ops.py)

Shadow means: **no live broker order, no real position or account mutation,
no broker authentication, no scheduler installation.** Order intents are
recorded locally, fills are simulated conservatively, P&L is shadow-only,
and every effect lives under the reset-safe, gitignored
`data/runtime/shadow_qualification/` root.

## Live-boundary statement

- The broker mutation surface is deny-listed in `.claude/settings.json`
  (validator: `scripts/validate_broker_permissions.py` → PASS, 0 ask-gated).
- The production receipt registry stays an immutable empty mapping
  (`scripts/evidence_schema.py:82`); a session without a verified catalyst
  remains `PRELIMINARY_CATALYST_UNVERIFIED`, and shadow sessions mirror the
  gate: `catalyst_verified=false` ⇒ **zero intents** — the gate is read,
  never weakened to produce more trades.
- `execution_enabled=false` everywhere declared and enforced.
- No launchd job exists for shadow; `scheduler_preview.py` renders a
  lint-checked PREVIEW artifact only (retry budget 0, catch-up NONE,
  RunAtLoad false). Installing is a separate user-approved operator action.
- Every intent row carries `live_order_submitted: false` and every fill
  `live_fill: false`; reconciliation fails on any violation.

## Engine guarantees

Deterministic content-addressed run ids; idempotent start with duplicate-run
prevention; gates before any intent (kill switch → trading day → data
freshness → catalyst → real risk gate via `pretrade_parity.check_intent`,
which calls `scripts/lib/risk_gate.validate` VERBATIM with the PROTECTED
limits — caller loosening is ignored by the gate itself); duplicate-intent
prevention (content-addressed intent ids); **side-aware** conservative
adversarial fills (a long pays `base*(1+half_spread+slippage)`, a short
receives `base*(1-half_spread-slippage)`; partial/missed fills are recorded
outcomes; an unsupported side is refused, never treated as a long);
append-only hash-chained ledgers (runs/intents/fills/events)
with tamper/truncation/rollback detection; trusted outer-clock receipts
binding each session to HEAD and the lake readiness receipt (the missing
piece `strategy_shadow_forward.py` names); crash/replay idempotence;
reconciliation; factual failure classes; retry budget zero — every attempt
is a visible ledger row.

## Kill switch

`data/runtime/operator_control/SHADOW_HALT`: file exists ⇒ the guarded entry
points refuse before any write (fail closed, including unreadable-marker).
Create it to halt; delete it to permit.

**Exact scope (corrected 2026-07-28).** The marker is checked by
`check_intent`, `ShadowSession.start`, `ShadowSession.propose_intent`,
`ShadowSession.complete`, and the verification wrapper. It is deliberately
NOT claimed for every entry point: `simulate_fill`, `mark_to_market`,
`heartbeat` and `soak_rollup` operate on an already-gated session. An
earlier revision of this document asserted "every entry point", which the
code did not implement; `complete()` has since been brought under the
marker because it is the call that advances soak evidence.

## Pre-trade control parity (Phase 12)

`pretrade_parity.parity_report()`: **10 enforced / 4 partial / 3 absent**
of the 17 controls. Five carry explicit ratification decisions (absolute
notional cap, share-quantity cap, duplicate-order idempotency for live,
numeric concentration threshold, operator kill switch for live); the
declared-but-unlocated timing-window enforcement
(`config/risk_limits.yaml` `timing:` block, not parsed by `risk_gate.py`)
is additionally recorded in the packet's open decisions. No limit was
invented or changed.

## Token economics (Phase 14)

Qualification and shadow lanes are 100% deterministic — zero model calls by
construction (`token_ledger.record_deterministic_lane`). The existing model
lanes get proxy ledger rows (tokens null — the CLI is invoked without a
usage envelope today). The 7c8de39 token-optimization savings remain
**claimed, unvalidated** (zero post-apply mornings); measurement protocol:
compare the next 5 natural sessions' ledger rows against the 07-16..07-23
baseline.

### Proposed operator pack (NOT built in this milestone): token capture

Change each runner's `claude` invocation to `--output-format json`, parse
the usage envelope (input/output/cached tokens, cost) in a deterministic
post-step, append to the token ledger. Deny-only; preserves
`audited_claude.py` FORBIDDEN_ARGS; alters no gate. Until applied, the
ledger stays in proxy mode and no savings claim graduates to "measured".

## Soak honesty

`soak_rollup` counts only real recorded shadow sessions; `target_sessions`
stays null until Charles ratifies a duration. The five-morning production
canary counter advances only from real scheduled post-hardening runs with
valid receipts — 2026-07-24's refused morning (rc=78) means the count is 0
and the first possible soak morning is the next clean scheduled trading-day
run.

**Receipt-derived soak (F4, 2026-07-28).** A completion is counted only when
the whole evidence chain exists for the same run id: a `run_started` row, an
outer-clock receipt, a `session_complete` row, matching fingerprints, and a
non-duplicate session date. `complete()` now refuses when the kill switch is
armed or when no `run_started` row exists, and `reconcile()` reports
`no_run_started_row`. Previously `complete()` could be called with no
`start()`, no gates and no work, which manufactured soak days out of nothing
(five completions against zero starts). The rollup additionally classifies
every counted session by provenance and reports `rejected_completions` with
reasons. (**Superseded by V3**, below: the default is now `unavailable`, not
`synthetic`, the vocabulary also carries `historical`, and the ladder consumes
`soak_eligible_sessions` rather than `completed_sessions`.)
*Corrected (V5R, R2-5):* the sentence that stood here — "A caller-supplied
completed count is never trusted" — overstated what the consumer can do. The
qualification reader refuses an **internally inconsistent** rollup (count vs
completed, ledger-integrity flag, sample list), but those checks read fields of
one caller-authored file: they are internal-consistency checks, **not**
independent verification, and a self-consistent forgery passes them. What
actually bounds a forged count is the unconditional self-attestation ceiling in
`qualification.evaluate()`.

## Signed position accounting (F3, 2026-07-28)

`side` is authoritative, not decorative. Before this fix it was read only for
identity hashing, so a short received a FAVOURABLE fill and a sign-inverted
P&L: a profitable short covering below entry was booked as a loss (the
reviewer counterexample reported approximately −52.37).

`ShadowSession.position_book()` replays this run's fills into signed
positions and supports open/increase (weighted-average entry), partial close
(proportional basis reduction, realized P&L on the closed quantity only),
full close, and flips (close the existing side first, realize on the closed
quantity, then open the opposite side with the excess only).

`mark_to_market` reports these separately and never conflates them:

- `gross_realized_pnl`
- `transaction_costs` (commission + borrow/carry, entry side)
- `net_realized_pnl` = gross realized − costs
- `gross_unrealized_pnl`
- `net_total_pnl`
- `marks_unavailable` / `complete`

**Still not modeled, disclosed:** exit-side friction. `net_*` here is net of
modeled ENTRY costs only, and must not be read as net of round-trip cost.

## Short-policy boundary (R1, 2026-07-28)

`CLAUDE.md` §1.5 lists short selling among **prohibited instruments** ("Long
US-listed common stock and unleveraged ETFs only"). That policy is unchanged and
was not modified by this work.

**Accounting support is not short-trading authorization.** The side-aware signed
arithmetic exists so that signed positions are computed *correctly when they are
encountered*: synthetic fixtures, imported/historical ledger reconciliation,
shadow defensive correctness, and detection of an unexpected negative position.

`ShadowSession.propose_intent` now refuses to OPEN any side in
`POLICY_PROHIBITED_OPENING_SIDES` with failure class
`policy_prohibited_instrument`, before any intent is recorded. The refusal
carries `counts_toward_soak: false`, and `soak_rollup` republishes `POLICY_NOTE`.

**Correction (F07, 2026-07-28).** The paragraph that stood here previously said a
caller "may pass `synthetic_accounting_only=True` … its intents are excluded from
soak evidence", and the section closed by asserting that "no scheduled short
intent exists". Both were false as written, and the review confirmed it:

- the escape was an unconditional caller-supplied boolean, so a session declaring
  `provenance="scheduled"` could pass it and open a short;
- the resulting intent row was byte-identical to an ordinary long intent — no
  marker recorded that the policy had been bypassed;
- that session still completed and still counted as a soak day, while
  `soak_rollup` republished a `POLICY_NOTE` stating shorts are prohibited.

The escape is now **bound to the session's own recorded provenance**. It is
honoured only when the session's `run` row declares `provenance == "synthetic"`;
under any other provenance the intent is refused with reason code
`synthetic_escape_requires_synthetic_provenance` and `counts_toward_soak: false`.
The declared provenance is read back from the hash-chained run ledger, not from
the `propose_intent` call, so the caller cannot supply it at the same moment it
asks for the exception. When the escape *is* honoured, the intent row carries
`synthetic_accounting_only: true` and `policy_exception: <POLICY_NOTE>`, so the
bypass is permanently visible in the append-only ledger rather than invisible.

What is now true and tested: no broker is contacted on any path, no paper or live
short is possible, and no session recorded as `scheduled` or `manual` can carry a
short intent by any argument combination. A session whose short intent was refused
still completes and still counts as a session in `soak_rollup` — it simply
contains no short intent. This is not a claim about the qualification ladder;
soak evidence remains an offline construct with no production producer.

## Export boundary (R2, 2026-07-28)

Two defects are closed:

1. **Diagnostics no longer echo secret-like keys.** The redaction decision now
   includes `FORBIDDEN_KEY_RE`, not only the value scan. Previously exactly the
   keys classified sensitive *by name* (`account_*`, `password_*`, `last_four_*`)
   were printed unredacted into `LeakageError`, because `\b\d{9,}\b` requires a
   word boundary and `_` is a word character.
2. **The numeric rule no longer refuses legitimate metrics.** It tests the
   integer part only, matching its documented intent. Counting every digit in
   `repr()` had refused ordinary unrounded research output — a PSR of
   `0.9772498680518208` has 17 digits — while still catching account-shaped
   values in both integral and non-integral form.

`scan_name` and `scan_bundle_inventory` extend the boundary to **filenames and
archive member names**, which were previously unchecked. Findings report a
bounded member index, a stable category and a stable reason code; the offending
name is never reproduced.

## V3 — provenance is declared, and the ladder is no longer provenance-blind

Authorized as Tier 1-A. **Tier 3 remains excluded**: no production consumer, no
scheduler registration, no paper or live activation, no broker, no order, no
capital allocation.

**What V2 actually shipped.** `ShadowSession.start` declared
`provenance: str = "synthetic"`. Omitting the argument was therefore treated as
a positive declaration of synthetic provenance — which is exactly the
precondition the synthetic-accounting escape checks. The escape was not bound to
a deliberate declaration; it was bound to a default. A session could then hold an
**open prohibited short intent**, complete, and increment
`completed_sessions` — the number `qualification.evaluate()` read — so synthetic
evidence advanced the ladder. `by_provenance` existed but no consumer read it.

**What is true now, and tested:**

1. `start()` defaults to `"unavailable"`, never to `"synthetic"`. An undeclared
   session is recorded as `unavailable`, cannot obtain the escape, and is
   ineligible for soak by construction. Omission can no longer be laundered into
   evidence.
2. `PROVENANCE_CLASSES` is a closed vocabulary — `scheduled`, `manual`,
   `synthetic`, `historical`, `unavailable` — and remains case- and
   whitespace-sensitive and fail-closed.
3. **Completion is refused while a policy-prohibited intent is open**
   (`reason_code: open_prohibited_intent`). A session carrying a short cannot
   become a completed session at all, so it cannot reach any counter.
4. `soak_rollup` publishes `soak_eligible_sessions` — valid completions whose
   provenance is in `SOAK_ELIGIBLE_PROVENANCE`, currently `("scheduled",)`.
   `completed_sessions` is retained as a factual count and is **no longer the
   ladder input**.
5. The ladder consumes `soak_eligible_sessions`. A rollup that does not publish
   an eligibility decision — including any pre-V3 rollup — is **not soak
   evidence** and fails closed rather than being read as if every completion had
   been scheduled.
6. Synthetic, manual and historical sessions remain fully usable for tests and
   defensive accounting. They are recorded, bucketed and distinct; they simply
   cannot advance a paper or live readiness rung.

Side-aware signed arithmetic remains defensive math for correctly computing a
position that is *encountered*. It confers no short-trading authority, and
`POLICY_PROHIBITED_OPENING_SIDES` and `SHORT_SIDES` remain the same set, so
nothing that produces short arithmetic can bypass the policy gate. No broker is
contacted and no scheduler is invoked on any path.

## V4 — side classification by sign, and a cross-checked soak consumer

Three verified V3 defects, each reproduced before editing.

**T1-E — the policy gate classified by literal membership.** `propose_intent`
tested `side in POLICY_PROHIBITED_OPENING_SIDES`, a three-element tuple, so
`sell_to_open`, `sell-short`, `SELL SHORT`, `short_sell` and a Cyrillic
homoglyph were all **accepted** into a `scheduled` session, which then completed
and counted as soak evidence. That directly defeated the V3 sentence "a session
carrying a short cannot become a completed session at all". `side_sign()`
already failed closed to `0` on anything unrecognised; the gate simply never
consulted it. It now does: an unclassifiable side is refused with
`unsupported_side` **before any intent row is written**, a recognised short
keeps its own `policy_prohibited_instrument` class, and long sides are
unaffected. The refusal moved earlier — V3 admitted these to the ledger and only
refused at `simulate_fill`.

**T1-F — the ladder trusted a number it published as untrusted.** `soak_rollup`
recomputes its counts from the ledger, but the document it writes is
model-writable, so at `qualification.evaluate()` the eligible count is
caller-supplied. V3 consumed it verbatim: a file containing only
`{"soak_eligible_sessions": 20}` — no ledger, no session ever run — passed the
gate, while that same artifact published *"a caller-supplied count is never
trusted"*. The consumer now cross-checks, over data the producer already emits:

- `soak_eligible_sessions <= completed_sessions`
- `ledger_integrity_ok is True`
- `len(eligible_sessions) == soak_eligible_sessions`

and refuses any internally inconsistent rollup. The note in the artifact now
describes what actually happens rather than asserting a guarantee made
elsewhere.

**T1-J — the fingerprint check was dead on the real path.** `soak_rollup`
compares start-row against completion-row fingerprints, but `complete()` never
wrote them, so the completion side was always `None`. Completion now carries
them.

*Corrected 2026-07-29 (V5 D08).* V4 claimed this means "a mid-session
fingerprint drift is detected end to end." **That is false.** `run_id` is a hash
of the fingerprints, so a genuine drift produces a **different run id and a
different session** — there is no such thing as mid-session drift on the real
path. What writing the fingerprints actually buys is detection of a **hand-edited
or imported ledger row** whose completion disagrees with its start. That is
worth having, and it is all that is claimed.

None of this changes the live boundary: no broker is contacted, no scheduler is
invoked, side-aware arithmetic remains defensive math, and short selling remains
prohibited by `CLAUDE.md` §1.5.


## V5 — canonical intents, real accounting, and a narrowed writer claim

**D06 — `complete()` was a no-op refactor.** A reviewer proved by exhaustive
probe that V4's `side_sign(x) < 0` is **identical on every input** to the V3
literal membership test, because `SHORT_SIDES` and
`POLICY_PROHIBITED_OPENING_SIDES` are the same tuple. Reverting it survived the
whole suite. An intent the classifier cannot recognise (`sign == 0`) still
completed and counted as a scheduled soak day — and the module's own docstring
names "imported/historical ledger reconciliation" as in scope, so V3-era ledgers
contain exactly those rows. Completion now refuses on `side_sign(...) <= 0`:
unknown is refused alongside short.

**D07 — signed accounting never consulted the declared side.** `position_book`
and `mark_to_market` trusted the fill row's `signed_quantity`, so a planted row
was booked as a short and marked to market at +200 P&L under a republished
"CLAUDE.md prohibits short selling" note. Every fill is now cross-checked against
`side_sign(declared_side)`; a disagreeing or unclassifiable row is **excluded
from the book and reported** in `unclassified_fills`, never silently booked.
This is what makes the sentence "nothing that produces short arithmetic can
bypass the policy gate" true; in V4 it was not.

*Corrected (V5R, R3-F1/F2/F3 + R4-A):* the V5 cross-check above was itself
bypassable — it derived `actual` only for real `int`/`float` values and then
`float()`-coerced the **same raw field** twelve lines later, so a declared
`buy_long` with quantity `"-100"` short-circuited the check and was **booked as
a −100 short with +200 P&L, completed, and counted as a scheduled soak day**;
`"not-a-number"` raised `ValueError` out of the real path; NaN was booked and
persisted into `shadow_pnl_rollup` as a non-strict JSON literal; and a non-dict
`intent` raised `AttributeError` from `complete()`. V5R validates every
untrusted quantity boundary through one typed validator (strings — including
numeric-looking strings — booleans, NaN, infinities and malformed objects are
refused, never coerced), the cross-check and the booking arithmetic consume the
same validated value, exclusions are **visible** in the persisted rollup
(`excluded_fill_rows`, schema 1.2, `complete=false`), a session with an
unclassifiable or untyped fill row **cannot complete or count toward soak**,
and the ledger serializer is strict (`allow_nan=False`) so a non-finite row is
refused before any bytes are written. Side-aware accounting for typed,
validated synthetic inputs is unchanged — accounting support remains
**not** short-trading authorization.

**D09 — *Corrected (V5R, R5-B1/B3): the narrowing rationale was FALSE and the
validator is restored.*** The V5 paragraph that stood here claimed "no
caller-controlled string reaches `facts`". Reviewer 5 disproved it:
`preview_dir` **is** caller-controlled, and `plutil -lint` echoed the plist
path into `facts["plutil_output"]`, so a destination named
`SAFE_TO_TRADE-LIVE_APPROVED` had **forbidden labels published to disk** — at
the V5 base the identical call raised `ForbiddenLabelError` and wrote nothing.
The validator V5 deleted was reachable and load-bearing. V5R restores real
validation at this writer, ordered **before any destination effect**: the
destination string is validated first; the plist is linted in a private temp
directory with the echoed path scrubbed (closing the injection channel
itself); the complete facts document and plist text are validated; only then
is anything written. A refusal leaves the destination absent or unchanged.
Disabling the call or moving it after the write are killed mutations, and the
"every writer validates" sentence in `__init__.py` is true again — proved
behaviorally per writer, never by a source grep. (V4's genuine defects in the
old call — post-write ordering, grep-only binding — remain fixed by
construction.) `render_preview` also validates its `hour`/`minute` slot and
still never installs anything.

**D10 — machine authority labels are ASCII-only.** The hand-written confusable
table is deleted. V4's table was commented as covering the letters that occur in
the forbidden labels; it did not — `T, M, B, Y, G, F, L, N` were all uncovered,
so four labels with Cyrillic homoglyphs were published to disk, and the test
parametrized exactly the three letters the table did carry. Any non-ASCII
character in a machine-label position is now refused outright, with no attempt to
normalize it into an allowed value. Human-readable description text remains
Unicode and is sanitized separately.
