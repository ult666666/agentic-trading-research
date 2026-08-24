# Research Qualification Engine (sim/research_qualification/)

Deterministic, stdlib-only, model-free integration layer over the existing
quant lab. It adds NO new statistical machinery where the lab already has it
(walk-forward, purge/embargo, locked holdout, PBO, DSR, HAC, bootstrap, SPA,
cost model, portfolio simulation — all in `scripts/quant/`, all tested);
it adds the five genuinely missing metrics, the lifecycle ladder, the trial
registry, lineage hard-gates, and the ratification packet.

## The six-state ladder (`qualification.py`)

`RESEARCH_NOT_READY → RESEARCH_QUANTIFIED_NEEDS_RATIFICATION →
RESEARCH_RATIFIED_SHADOW_READY → SHADOW_SOAK_IN_PROGRESS →
PAPER_READY_NEEDS_RATIFICATION → LIVE_CANDIDATE_NEEDS_FRESH_APPROVAL`

Pure fail-closed reader over: lake readiness receipt, feature corpus, grid
worker + research factory + artifact stager health, natural-run acceptance
rollup, the shadow soak rollup, and Charles's ratification record. Forbidden
labels (LIVE_APPROVED, PROFITABILITY_GUARANTEED, SAFE_TO_TRADE,
AUTOMATIC_LIVE_ACTIVATION) are not representable. **Current honest state:
RESEARCH_NOT_READY** (pinned by a regression test): zero eligible corpus
rows, `research_ready=false` with 8 data blockers, registry genesis-only.
The critical path to any higher state is DATA (corporate-actions rights,
broad daily history incl. SPY, delisted coverage), not methodology.

## Threshold provenance

Ratified (inherited verbatim, source recorded): the
`config/research_factory.json` evidence gates (DSR ≥ 0.95, PBO ≤ 0.2,
min 100 OOS / 30 holdout, PF ≥ 1.1, drawdown ≤ 0.15, 2× cost stress, SPA
α=0.05, uniqueness deltas…) and everything in PROTECTED
`config/risk_limits.yaml`. Absent thresholds (MinTRL confidence,
regime slice minimum N, dollar-capacity floor, shadow/paper durations,
minimum live-candidate trades, portfolio CVaR floor) are emitted as
proposals with `status=NEEDS_CHARLES_RATIFICATION` — never silently chosen.

## No self-ratification

`ratification_packet.py` structurally refuses to emit
`ratified_by_charles=true` or `signature_source="charles_manual"`
(`SelfRatificationError`). Ratification happens only when Charles manually
writes `data/runtime/shadow_qualification/ratification/
charles_ratification.json` with `signature_source="charles_manual"` and
every required decision individually `ratified: true`;
`qualification.load_ratification` rejects anything else.

## Lineage hard-gates (`lineage.py`)

Closes the audit's four fail-opens: null `git_commit` blocks; every dataset
content hash must resolve in a provenance index built from REAL artifacts
(lake manifests / readiness receipts / `data/MANIFEST.json` / explicit
tracked-fixture allowlist — declared provenance in the manifest can never
override the index); currency and bar-frequency are explicit; structured
look-ahead/survivorship check objects (a bare "yes" blocks); revised data
can never claim `known_at_decision_time`.

## Trial registry (`trial_registry.py`)

Content-addressed experiment identities over strategy family + parameters +
data/code/config fingerprints + pre-stated hypothesis + windows. Hash-
chained append-only ledger: deletion/renumbering breaks the chain and
freezes the registry; outcomes for unregistered ids are refused; retries
are explicit attempt rows; OOS-window reuse as training on the same data
fingerprint requires naming the parent experiment. Reports always cover
every trial — winners-only reporting is structurally impossible.

## Verification wrapper (`verify.py`)

One documented command: `python3 -m sim.research_qualification.verify`
runs `bash scripts/run_test_suite.sh` (fixed argv, no retry), preserves raw
output, and emits one machine-readable summary receipt (counts, failed test
ids, leftover-temp check) under the reset-safe runtime root. Durations stay
in diagnostics, never in the deterministic evidence body.

## Executable evidence contract (F1, 2026-07-28)

`evidence_contract.py` closes the producer/gate vocabulary. The gate
previously accepted `{"complete", "evaluated"}` while `scripts/quant/
grid_worker.py` can only emit `{blocked, bundle_created, bundle_existing,
corpus_not_reviewable, corpus_unavailable, data_not_ready,
insufficient_settled_sample, insufficient_temporal_sample,
selection_bias_unavailable}` — an intersection of exactly zero. The passing
branch was therefore unreachable through real evidence and reachable only
through a hand-authored status.

Now: a status outside the producer's vocabulary is classified `unknown` and
can never pass; a producer refusal fails; and an evidence-capable status
(`bundle_created` / `bundle_existing`) yields `NOT_RUN` — not PASS — unless a
validated OOS artifact accompanies it. **A bundle existing is not evidence
that out-of-sample evaluation happened.** The artifact must carry an
experiment id, strategy/config/data fingerprints, an OOS window, evidence
status, the required metric set, a trial-registry reference, a lineage
reference, and a self-consistent content hash; provenance sets come from the
caller's real registry and lineage state, never from the artifact itself.
`test_evidence_contract.py` pins the contract against the producer's
committed schema, so drift on either side fails loudly.

## Wired lineage and trial registry (F2, 2026-07-28)

`lineage.py` and `trial_registry.py` were previously referenced only by their
own tests: `qualification.py` mentioned neither, and the ladder could reach
`RESEARCH_QUANTIFIED_NEEDS_RATIFICATION` with no validated lineage and no
registered experiment. Both are now real gates in `evaluate()` and real
inputs in `collect_inputs()`. Missing lineage, invalid lineage, a missing
registry, a failed integrity check, or a winners-only registry (every
registered trial selected, i.e. failed attempts never recorded) each block
quantification.

## Net-of-cost enforcement (F6, 2026-07-28)

Loading or publishing a threshold key is not enforcement. Previously the
ladder read seven ratified thresholds, published their NAMES, and compared
none of them; no gate read any performance quantity, so a strategy whose
costs turned a +25 bps gross into a −8 bps net still advanced.

`net_of_cost`, `model_risk` and `cost_completeness` are now gates that
compare observed metrics against the exact ratified values. Every comparison
is emitted in a machine-readable `gate_trace` (input metric, threshold key,
threshold value, ratification status, observed value, comparison, result),
and `thresholds_enforced` names the thresholds actually compared — a strict
subset of `thresholds_ratified`. An absent, malformed or unratified
threshold yields `NOT_RUN` and keeps the strategy unqualified; a missing
PBO/DSR yields `NOT_RUN`; no default is ever invented.

## Offline research core — scope statement (2026-07-28)

**This package is an OFFLINE RESEARCH QUALIFICATION CORE.** Stated factually, so
no reader infers more than exists:

- `production_consumer_present = false` — no module outside
  `sim/research_qualification/` imports this package. Verified by an AST
  import/call graph over 430 repository Python files: zero external importers.
- `production_runner_present = false` — the package's only `__main__`
  (`verify.py`) is a wrapper around `scripts/run_test_suite.sh`, i.e. a test
  runner, not a qualification entrypoint.
- `scheduler_registration_present = false` — no launchd plist and no entry in
  `config/autonomy_workflows.json` references this package.
- `oos_evidence_producer_present = false` — nothing in the repository writes
  `data/runtime/quant/oos_evidence.json`.
- `broker_write_authority = false`, `live_activation_authority = false`.

**Production integration is DEFERRED** and remains a separate Charles decision:
either (A) bind this core to an existing production lane, or (B) create a runner
and separately approve its scheduler registration. Neither is authorized, and
neither is implied by any test in this package passing.

The controls below are correct *offline* contracts. Their tests prove the
controls behave correctly when invoked; they do not prove — and must not be read
as proving — that anything in the running system invokes them.

## Hardened offline contracts (2026-07-28)

**Exact run binding (I1).** An OOS artifact must agree, by exact equality, with
the run's identity across `RUN_BINDING_FIELDS`: config, data and code
fingerprints, registry identity, experiment id and trial reference.

*Corrected 2026-07-28 (V3).* This paragraph previously also listed strategy and
cost-model fingerprints. They were removed from `RUN_BINDING_FIELDS` in V2
because no offline producer emits them, and the sentence was not updated — it
described a binding the code no longer performed. Those two fields are
**required on the artifact** (`REQUIRED_EVIDENCE_FIELDS`, restored in V3, so
they are part of `content_sha256`) and are **cross-checked for equality only
when both sides supply them** (`OPTIONAL_BINDING_FIELDS`). Required content and
exact-equality binding are different obligations and are now stated separately.
Presence is never sufficient. When the expectation cannot be assembled at all,
`oos_evidence_run_binding_unavailable` blocks — previously an empty expectation
silently skipped the check.

**Registry path integrity (I2, corrected 2026-07-28).** `TRIAL_REGISTRY_PATH` is the
single source of truth and BOTH reader branches derive their suffix from it via
`relative_to(REPO_ROOT)`. An earlier revision claimed reader and writer "cannot
diverge" while the explicit-`repo_root` branch silently dropped the
`shadow_qualification` segment; that is fixed. (The V2 claim "and
regression-tested" was false: the test asserted `src is None or ...` and the
first disjunct always held, so reverting the fix left the suite green. The
replacement test is landed separately in this round.) It is not cwd-dependent. The registry contributes
`registry_identity` — a digest of the resolved path, never the path itself — so
the registry participates in evidence identity without publishing a private path.

**Threshold coverage (I3).** `ALL_THRESHOLD_GATES` is a closed map over
net-of-cost, model-risk, sample-adequacy and drawdown groups. A ratified numeric
threshold that no gate compares is a **coverage defect**: the `threshold_coverage`
gate fails and the emitted `threshold_coverage` block lists exactly which
thresholds were loaded but never compared. `thresholds_enforced` is derived only
from comparisons that actually ran.

**Non-finite rejection (I4, corrected 2026-07-28).** NaN, +Infinity and -Infinity are refused at every
untrusted numeric boundary — metrics (all of them, not only the required set),
thresholds, and cost components. The evaluator's output is strict-JSON serializable
**including when an input metric is non-finite** — the gate trace withholds the
value and records `value_note: non_finite_value_withheld` rather than emitting a
bare `NaN`. An earlier revision asserted serializability unqualified while only
the clean case had ever been exercised. Ladder COUNT inputs
(`eligible_row_count`, `broker_canary_eligible_count`, `completed_sessions`) are
validated as finite non-negative integers; `+Infinity` previously passed them.
Input parsing rejects the non-standard `NaN` / `Infinity` literals.

**Cost completeness in identity (I5).** `cost_completeness` is a required
evidence field and therefore part of the artifact's content hash: a complete-cost
and an incomplete-cost artifact can never share an identity. When completeness is
declared, every component in `COST_COMPONENT_KEYS` must be present, finite, and
sum to `total_costs`, and `net = gross − costs` must hold. An artifact that does
not declare complete costs cannot satisfy a net-of-cost gate.


## PSR gate — DROPPED (Charles decision, 2026-07-28)

`minimum_probabilistic_sharpe_probability` is **not** a required offline
qualification threshold. It is absent from `ALL_THRESHOLD_GATES`, from the
threshold-coverage map, from the ratification request, and from every state
transition. It was never present in `config/research_factory.json`, and that
file is **not** modified by this work.

PSR is retained only as an **OPTIONAL_INFORMATIONAL_METRIC**: a finite `psr`
value may be reported for information, its absence never blocks on its own, and
a non-finite `psr` is still refused by the metric sweep. No default is inferred
and no ratification is implied. PBO, DSR, sample-adequacy, trade-count,
cost-completeness and drawdown gates are unchanged by this decision.

## Producer binding — corrected scope (2026-07-28)

`RUN_BINDING_FIELDS` now contains only fields an offline **producer** can supply:
`config_fingerprint`, `data_fingerprint`, `code_fingerprint` (lineage),
`registry_identity` and `trial_registry_ref` (emitted by
`TrialRegistry.report()` itself), and `experiment_id`. `strategy_fingerprint`
and `cost_model_fingerprint` are `OPTIONAL_BINDING_FIELDS` — cross-checked when
both sides supply them, never required, because no producer declares them.

Values found on the grid-worker health artifact **cross-check** lineage and can
never override it. That artifact's committed schema sets
`additionalProperties: false` and declares no fingerprint properties, so
anything found there is caller-authored; promoting it turned an independent
binding into self-agreement.


## V3 — Tier 1 and Tier 2 closure (2026-07-28)

Authorized by Charles as Tier 1 + Tier 2. **Tier 3 remains excluded**: no
production consumer, no scheduler registration, no paper activation, no live
activation, no broker connection, no order authority, no capital allocation, and
no claim that this offline core is production-integrated.

**Cost materiality (T1-B).** The zero-cost refusal was written as the literal
`costs == 0`, which had no magnitude floor, so `total_costs=1e-18` restored
`cost_completeness: PASS` and let gross be relabelled as net. The module now
names its single numeric contract once — `NUMERIC_TOLERANCE`, the value the
cost identity was already evaluated with — and reuses it. A declared aggregate
cost at or below it leaves `gross - costs` indistinguishable from `gross` at the
precision the identity is checked with, so it is refused as immaterial. No
second, inconsistent tolerance was introduced, and this is an internal
evidence-quality boundary, not a Charles-ratified trading threshold.

**Evidence contract (T1-C).** `strategy_fingerprint` and `cost_model_fingerprint`
are restored to `REQUIRED_EVIDENCE_FIELDS`. V2 removed them beyond its
authorization, which meant two artifacts produced under *different cost models*
were byte-identical and shared a `content_sha256` — the identity that
`cost_completeness` and I5 depend on.

**Grid/lineage disagreement is now consumed (T1-C).** V2 replaced a wrong
override with a `_grid_disagreement:<key>` marker that nothing read, so a
producer artifact contradicting the lineage manifest about which data was used
was silently accepted. The marker is now consumed by `validate_oos_evidence` —
the same path that decides qualification state — and yields
`oos_evidence_grid_lineage_disagreement:<field>`, failing the gate closed.

**Real-path PSR (T1-D).** The `psr_benchmark_sr` proposal is removed from
`collect_inputs`. V2 asserted it was inert by measuring a fixture whose proposed
set was already empty; on the real path it was emitted into `open_decisions`
every run, asking Charles to ratify a benchmark for a gate he had dropped. The
stale `PBO/DSR/PSR unevaluable` evidence string is corrected to `PBO/DSR`.

**Real vs fixture threshold counts (T1-D).** The real path currently enforces
**0** thresholds and reports `model_risk: NOT_RUN`, because no OOS metrics
exist. A fixture supplying complete evidence enforces 12. Both are true; V2
reported the fixture number without its qualifier. Reports must state the real
count.


## V4 — Tier 1 evidence and cost integrity (2026-07-29)

Five independent reviewers rejected V3. These are the verified corrections.

**Cost materiality is now a RATIO (T1-A).** V3's bound was `costs <=
NUMERIC_TOLERANCE`, an **absolute** float-precision floor that the round's own
documentation called materiality. It was scale-blind: at `gross=1e8` a declared
cost of `6e-9` left `net` bit-identical to `gross` and passed, and at
`gross=0.25` a cost of `1.2e-9` — 4.8e-9 of gross — passed with the ladder
advancing. The bound is now `abs(costs) < abs(gross) *
MINIMUM_COST_TO_GROSS_RATIO` with the ratio pinned at **1e-6**, so it holds
identically at every scale, and it is enforced at **both** `evaluate()` and
`validate_oos_evidence` rather than in a single `elif`.

The ratio is an **evidence-quality floor** — it asks whether an artifact's net
is distinguishable from its gross, not whether a cost is economically
acceptable. A reviewer correctly observed that a materiality bound nonetheless
has economic content, so it is stated here explicitly rather than asserted away,
and it is a candidate for ratification if Charles wants it to be one.

**One tolerance, actually shared (T1-C).** `NUMERIC_TOLERANCE` now lives in
`evidence_contract` and is imported by `qualification`. V3 declared it in
`qualification.py` while `evidence_contract.py` kept two hardcoded copies of the
same literal, so the "single authoritative numeric contract" claim was false.
The regression test asserts the two modules share the same object and checks
behaviour at the contract boundary, not source text.

**The cost-model fingerprint is now bindable (T1-H).** It was a required field
with **no format contract**, so `"LIES"`, `"0"` and `" "` all satisfied it, and
`OPTIONAL_BINDING_FIELDS` cross-checked it against a lineage key that
`lineage.REQUIRED_FIELDS` did not define — so on any *producible* manifest the
cross-check could never execute. Both fields are now required of lineage and
carry the hex64 contract. Restoring them in V3 forced a *declaration* into the
content hash; only now is that declaration bound to a fact.

**Grid disagreement covers every binding key (T1-I).** V3 checked three; a grid
artifact contradicting lineage on `code_fingerprint`, `experiment_id`,
`registry_identity` or `trial_registry_ref` produced no marker and passed.

**Malformed artifacts fail closed (T1-G).** `_read_json` returned whatever
`json.loads` produced, so a list-shaped runtime artifact reached `evaluate()`
and raised an unhandled `AttributeError` out of the real `collect_inputs` path.
A non-dict top-level value is now treated as absent.


## V5 — self-attestation made explicit (2026-07-29)

Five independent reviewers rejected V4. These are the corrections, each proved
by a mutation constructed **before** the production edit.

### The offline self-attestation ceiling (D04)

V4 shipped four "cross-checks" on the soak rollup. All four read fields of **one
caller-authored file**, so a self-consistent forgery with no ledger and no
session ever run reached `PAPER_READY_NEEDS_RATIFICATION`. A reviewer then
fabricated a ledger through the public API in 1.7 s whose real `soak_rollup()`
output was byte-shape-identical to honest evidence, and concluded — correctly —
that **no consumer-side check can distinguish the two**.

The remedy is not to make self-attestation trustworthy. It cannot be, from this
side. It is to cap what it may claim:

- `evaluate()` publishes `self_attested` and `external_evidence_verified`;
- a stable blocker `external_evidence_binding_unavailable` is emitted;
- the state is **capped below `PAPER_READY_NEEDS_RATIFICATION`** while no
  external attestation exists.

Gate results are unchanged — an internally consistent rollup still passes its own
gate. What changes is that internal consistency can no longer be presented as
readiness. Charles's ratification is an *authorization*; it is not external
verification of the evidence, so it does not lift the ceiling either.

*Corrected (V5R, R2-1/R2-2):* the V5 paragraph that stood here claimed the
ceiling "lifts on evidence, not on a code edit" through
`_external_evidence_verified()`. That function performed **no verification** —
it type-checked a caller-supplied dict, so a forged rollup plus
`{"verified": true, "producer_identity": "totally-made-up", "content_sha256":
"not-even-a-hash"}` reached the **top rung** while publishing two factually
false fields. The function and its `external_attestation` input are **deleted**:
the blocker `external_evidence_binding_unavailable` and the ceiling are now
**unconditional**, `self_attested`/`external_evidence_verified` are milestone
facts (`true`/`false`), and lifting the ceiling requires a **reviewed code
change** binding a registered independent producer — surfaced to Charles as the
`external_evidence_binding` open decision, not pretended away.

*Superseded in part (V6/V6R, Charles Decisions 2026-07-30).* The reviewed code
change now exists: the supervised post-close lane
(`com.charles.trading.postclose`, `scripts/post_close_finality.py`) publishes
one immutable, content-addressed `qualification_evidence_receipt.json` per
fully source-valid COMPLETED run, hash-linked to its terminal receipt.
`_verify_postclose_attestation` re-derives the **whole lane** consumer-side —
receipt and terminal self-hashes, the byte-level chain link, the cutoff
receipt, source contract, report and autonomy state the terminal binds
(nonexistent or re-hashed upstream files block), the canonical path of every
chain member, and byte-plus-parsed agreement with the very
oos-evidence/lineage/trial-registry artifacts the ladder judges (an in-place
ledger rewrite or an edit to any lineage field, mapped or not, blocks).

*Corrected (V6R-F1/F2, fresh independent final review, 2026-07-30).* Hash
agreement is not contract agreement: the first V6R consumer re-hashed the
upstream files but re-validated only a bounded subset of their **semantics**,
so a mutation the producer validator REJECTS could still retain consumer
binding PASS (demonstrated for a contract declaring `ok:false`, an
autonomy-state `operating_timezone` edit, and a forged cutoff temporal
ordering — each with the chain re-hashed consistently). Two parity gaps are
now closed:

- the consumer enforces the producer's full **source-contract** contract
  (key set, schema, target, `ok`/`errors`, safety, generation-vs-cutoff
  ordering, the embedded cutoff-status block, and the sha256 of the
  contract's own dependency artifacts — watchlist, current preflight,
  closing snapshot — whose bytes it now reads), the cutoff envelope's
  temporal semantics, the cutoff↔contract cross-bindings, and the producer's
  complete **autonomy-state** contract, re-deriving the state projection and
  requiring the terminal's stored projection to equal it;
- trial-registry semantics are **derived from the attested ledger bytes**
  (`report_from_ledger_bytes`), not trusted from a caller/report summary, so
  a fabricated `total_registered` / `selected_count` / `ledger_integrity_ok`
  / `ledger_rows` / trials list over honest bound bytes blocks. Unusable
  ledger bytes (malformed, duplicate-key, non-finite, pathologically nested,
  non-UTF-8, oversized) fail closed rather than degrade to the summary.

*Corrected again (GPT-F1/F2/F3, independent cross-model final review,
2026-07-31).* That round found three further defects, each reproduced by
execution before any edit, and none of them visible to hash agreement:

- **path shape.** Only the leaf was checked for a symlink, so relocating a
  bound source behind a **symlinked parent component** — target inside the
  repository or outside it — left the bytes and every receipt hash intact
  while the producer refused and the consumer still returned binding PASS.
  Both sides now read every canonical source through one bounded reader
  (`qualification.canonical_bounded_read`, `post_close_finality.
  canonical_bounded_read`) that requires a canonical repository-relative
  lexical path, refuses absolute paths and `.`/`..`/empty components,
  inspects **every** path component with lstat semantics, requires a regular
  file beneath the exact root, enforces the size bound, and returns bytes
  only after all of it — with a stable bounded reason otherwise.
- **nested types.** Unhashable values at enum and token boundaries
  (`overall_state`, the attempt state, an event name) raised `TypeError`
  straight out of both public APIs. Type is now validated before any
  membership test, and an event's machine tokens are validated as bounded
  strings before the chain comparison and before any of them may become a
  dict key.
- **collector order.** `collect_inputs` executed the live registry reader
  over untrusted runtime bytes before the strict verifier could run, so a
  chain-valid row missing `trial_id` escaped as `KeyError`. The collector now
  derives the report from bounded, canonically read raw JSONL, computes
  `registry_identity` separately from the canonical resolved registry path,
  and reports `registry_unreadable` for malformed rows, wrong-shaped bodies,
  unknown row schemas, invalid outcome states, duplicate keys, non-finite
  values, invalid UTF-8, pathological nesting, oversized ledgers, and
  symlinked registry paths.

Producer/consumer parity is asserted only where behavioral tests demonstrate
it; the shared reader contract and the enum/token guards are covered by
public-boundary oracles on both sides.

*Corrected again (GPT-R2-F1/F2/F3, second independent cross-model review,
2026-07-31).* That round found three further defects, each reproduced by
execution before any edit:

- **event tokens.** The consumer validated an event's `event` and `reason`
  but filtered on `workflow` without validating it, so an extra event for
  ANOTHER workflow carrying `workflow: []` was silently discarded
  consumer-side while the producer refused the whole state. One machine-token
  predicate now governs both sides — `event`, `reason`, `workflow` (bounded
  token AND a member of the five-workflow vocabulary), `run_id`, `seq`, and
  `utc` are validated BEFORE any filtering, comparison, projection, or
  dict-key use, and an invalid event anywhere in the sealed state rejects the
  binding.
- **writer-impossible registry rows.** A hash-chain-valid registration that
  no `TrialRegistry.register()` call could have produced (empty body, a
  `trial_id` that does not re-derive from the body, a duplicate or gapped
  trial number, an outcome for an unregistered trial, a rejected outcome with
  no reason, an attempt row carrying a dedupe key the writer never sets) was
  accepted and inflated the counts the non-winners-only gate reads. The
  writer's own predicates are now pure functions shared by
  `register()`/`record_outcome()` and by `report_from_ledger_bytes`, which
  additionally enforces content-addressed identity, referential integrity,
  contiguous ordering, and dedupe-key semantics. **A valid hash chain is not
  a valid registry**: a domain-invalid ledger returns one bounded semantic
  error, never a degraded report, and can never leave `ledger_integrity_ok`
  true.
- **registry trust (round 3).** Three further registry defects were found and
  reproduced before any edit. (a) The byte verifier enforced the writer's
  body, identity, ordering, and dedupe rules but NOT its OOS-reuse/lineage
  rule, so a registration `register()` refuses — a training window
  intersecting a prior same-fingerprint out-of-sample window with no
  acknowledged parent — verified when appended directly and moved
  `total_registered` from 1 to 2, flipping the anti-winners-only gate. The
  rule is now one pure function shared by the writer and the verifier, which
  reconstructs prior registrations from the ledger and requires EVERY
  conflicting parent to be named. (b) The anti-winners-only gate used
  `total_registered > selected_count`, which an **unevaluated** registration
  satisfied although no losing outcome existed; it now requires every
  registration to carry a terminal outcome (`total_with_outcome ==
  total_registered`) AND at least one explicit non-selected outcome from the
  writer's own vocabulary (`rejected`, `not_selected`, `abandoned`), and its
  evidence names the selected, non-selected, and unevaluated populations
  separately instead of asserting "all attempts recorded". (c) Ledger
  envelopes were not type-exact: `sequence: false` compared equal to 0
  (`bool` subclasses `int`) and a string `monotonic_ns` skipped the ordering
  check, both leaving `ledger_integrity_ok` true. One envelope validator now
  requires non-negative, non-bool integers with a contiguous sequence and a
  non-decreasing monotonic clock, applied before any semantic processing.
  Separately, the writer accepted a non-string `note` and a non-dict
  `metrics` that its own verifier later rejected; both shapes are now
  validated before the append, so **a writer cannot create a row its own
  byte verifier refuses**.
- **registry body and row contract (round 4).** Three more registry defects
  were reproduced before any edit. (a) The byte verifier called
  `isinstance(note, str)` / `isinstance(metrics, dict)` where the writer
  called `note_problem()` / `metrics_problem()`, so a forged row carrying a
  control-character note or a `{"flag": true}` metrics value — values the
  writer refuses — verified end to end; the verifier now calls the SAME
  predicates, proven on forged bytes rather than by writer refusal. (b) The
  registration contract checked little more than non-emptiness, and the
  interval normalizer silently DROPPED malformed windows and coerced values
  with `str()`, so a nonempty malformed training window disappeared before
  the OOS-reuse rule could see it, a bool fingerprint, a string
  `parameters`, and a bare-string `metric_set` were accepted, and
  `in_sample_windows = 1` raised an uncontrolled `TypeError`. One fail-closed
  registration validator now enforces exact types and shapes for every
  required field, and a closed window contract (both the dict form and the
  two-item list form — both genuinely part of the writer contract — with ISO
  dates and `start < end`, and no silent drop or coercion). (c) Both sides
  called `set()` directly on the untrusted `oos_reuse_acknowledged_parent`,
  so an object — or a list containing one — escaped as an unhashable
  `TypeError` from the writer AND the consumer; one closed parser now
  validates every item before anything is hashed and returns a bounded
  problem instead. **`trial_id` is unchanged** — the same eleven fields in
  the same canonical form — and no operational ledger exists, so no identity
  moves and no ledger migration is implied.
- **trial identity and ledger capacity (round 5).** Four more defects, each
  reproduced before any edit. (a) `_run_binding` combined lineage identities
  with only `bound_trial_id`, so **trial membership was being treated as OOS
  correspondence**: an out-of-sample artifact could name the selected trial
  while that trial's own registration declared different data/config/code
  fingerprints or a different out-of-sample window, and the lane still
  verified. The selected registration's identity is now derived from the
  verified ledger bytes and must agree with both the OOS artifact and the
  lineage manifest; the predeclared `metric_set` must also appear among the
  metrics the result reports (the OOS contract carries `metrics`, not
  `metric_set`, so no new required evidence field was invented). (b) Dict
  windows and two-item-list windows hashed to **two different trial IDs for
  one experiment**, which could manufacture an artificial selected/rejected
  pair; windows are now canonicalized before identity, dedupe, persistence,
  OOS-reuse comparison, and byte-verifier reconstruction, so the second
  registration is idempotent. No operational ledger exists in the primary
  checkout or any worktree, so this changed no stored identity and required
  no migration. (c) `json.dumps` silently coerced integer dict keys to
  strings and tuples to arrays, and an unknown top-level field was persisted
  while sitting outside `trial_id`; a recursive JSON-native validator now
  enforces exact types at every depth and the top-level field set is closed —
  `oos_reuse_acknowledged_parent` is the one supported optional field and is
  explicitly **non-identity metadata**, bounded, validated, and unable to
  move the trial id or the dedupe key. (d) The writer could append a row that
  pushed the ledger past the shared size cap, after which the verifier, the
  producer, and the collector all refused the registry; one capacity contract
  now projects the exact resulting size before the append and refuses early,
  with per-field bounds on serialized parameters, metrics text, and rejection
  reasons. **Every writer success now leaves a ledger every reader can
  read** — asserted by capacity tests, not by claim.
- **path language.** The producer constructed `Path(raw)` before validating,
  which normalizes away a leading `./`, a doubled separator, and an embedded
  `/./`, so the two sides accepted different raw forms; the producer would
  also still follow an in-repository symlinked parent for the quant sources.
  One raw-string validator now runs on both sides BEFORE any `Path`
  construction, and the producer's remaining canonical reads (contract,
  cutoff, terminal and qualification receipts, autonomy state, OOS evidence,
  lineage) go through the component-checking bounded reader. Agreement is
  asserted by an acceptance matrix that runs BOTH implementations over
  identical rows — the inventory is demonstrated, not claimed.

What a verified receipt does — and does not — change, per Charles's
2026-07-30 scope ruling:

- it binds the provenance of **exactly three** input families
  (`oos_evidence`, `data_lineage`, `trial_registry`), reported per-family via
  `externally_bound_families` / `self_attested_families`; the global
  `self_attested` stays `true` while ANY family is self-produced;
- the D04 state cap below `PAPER_READY_NEEDS_RATIFICATION` is **retained**
  under the named blocker `self_attestation_cap_retained:<families>`, because
  the families that gate the top rungs (`shadow_soak`, `acceptance_rollup`,
  ratification) remain unbound — a forged rollup plus a verified receipt can
  never publish a top rung with zero blockers;
- no quality gate, threshold, or trace result changes; the unratified
  `minimum_cost_materiality_ratio` (Decision A) continues to fail closed;
- a family document the collector itself proves source-less
  (`input_sources[family]` is null while the document is present) is
  **caller-authored** and floors the ladder at `RESEARCH_NOT_READY` under
  `caller_authored_input:<families>`.

**Trust boundary (Charles-accepted, 2026-07-30):** the receipt chain is
content-addressed but **unkeyed**. A writer with local write access to
`data/runtime/` could fabricate the entire lane consistently; consumer-side
re-derivation only forces the forgery to be total. Charles knowingly accepted
this residual for Decision B: a verified binding grants labeling/visibility
only, no downstream consumer derives any authority from it, and
`execution_enabled` is permanently false.

### Cost materiality bound to a declared serialization precision (D01)

*Corrected (V5R, R1-F3/F4/F5): the rule this section described is deleted.*

V3 used an absolute float-precision floor; V4 replaced it with a relative ratio.
Both were bypassable, in opposite directions — V3 at large gross (`1e-18`
passed), V4 at gross ≤ 1e-3, where the relative bound was swallowed by the
absolute identity tolerance and a net **declared bit-identical to gross** passed.

V5 bound the rule to the serialized money precision (`round(x, 6)`), claiming it
scale-correct by construction. Reviewer 1 disproved that: the verdict tracks the
**fractional position of gross**, not the size of the cost — a flat absolute
floor of ~5e-7 from `gross=1e-6` to `1e9` (V3's rejected design with a larger
constant), blind to sign flips through zero, refusing a cost **three times
gross** while passing a ratio of `1e-16`.

Three rounds derived a materiality bound from a technical fact and each was
disproven. **No pure function of `(gross, net)` can answer "is this cost
material" — the bound is economic and belongs to Charles.** V5R therefore:

- keeps only the factual **verifiability floor**: a cost claim at or below the
  committed identity tolerance (`NUMERIC_TOLERANCE = 1e-9`) cannot be
  distinguished from gross-relabeling *by the identity check itself* and is
  refused as `cost_below_identity_resolution` — a statement about the committed
  contract's resolution, not a materiality judgment;
- consumes an **economic bound only when ratified**
  (`minimum_cost_materiality_ratio`, enforced and traced in the
  `cost_completeness` gate, counted by `threshold_coverage`). No such bound is
  ratified today, so net-of-cost claims **fail closed** with
  `cost_materiality_bound_unratified` and the decision is surfaced in
  `open_decisions` with **no proposed value** — nothing is invented, nothing is
  marked ratified.

### Numeric and shape boundaries (D02, D03)

Magnitude is validated before any arithmetic, so a JSON-legal `10**400` fails
closed instead of raising `OverflowError`. No magnitude *ceiling* is imposed: a
mutation proved the ceiling in the first draft was not load-bearing, so it was
removed rather than kept and claimed. Every consumed sub-document — including
`oos_evidence.metrics`, which the top-level guard did not reach — is
shape-validated before use.

### Bounded list validation (D05)

V4's `and soak_sessions <= 50` **disabled** the list check above 50; a count of
1e9 with an empty list passed. The list must now carry exactly
`min(count, 50)` rows, and a count beyond the reportable maximum is refused
rather than exempted.

## V6R — atomic registry transactions and the strict byte contract (2026-08-01)

The GPT round-6 review proved with executions that every `TrialRegistry`
write was a read-then-append race: numbering, dedupe, conflict, OOS-reuse,
and capacity were all decided over an UNLOCKED snapshot before
`AppendOnlyLedger.append` took its lock. Concurrent registrations both
persisted `trial_number=0`; concurrent attempts both persisted
`attempt_number=1`; a losing conflicting outcome received the winner's row
as its own success; two near-cap writers both passed the stat-based
projection and pushed the ledger past the shared cap; and an identical
replay at capacity was refused before dedupe could return the existing row.

Every registry write now runs as ONE exclusive-locked transaction
(`AppendOnlyLedger.transact` in `state_safety.py`): the exact current bytes
are read once under the lock; the plan strictly validates them, resolves
dedupe/conflicts, and derives numbering and OOS-reuse from that locked
snapshot; the boundary builds the envelope, enforces `MAX_ROW_BYTES` /
`MAX_LEDGER_BYTES` against the EXACT encoded row, appends, and fsyncs — with
no lock release, second read, or stale-size decision in between. The
transaction surface is opt-in: `append()` callers (`shadow_ops`,
`token_ledger`) are unchanged. Proof is behavioral, not asserted:
deterministic barrier-gated public-boundary tests
(`test_v6r_concurrent_registrations_get_contiguous_numbers`,
`..._concurrent_attempts...`, `..._concurrent_conflicting_outcomes...`,
`..._concurrent_near_capacity...`, at the real 2 MiB cap) serialize two
writers and pin contiguous numbers, exactly-one-winner conflict semantics,
and a final ledger at or under the cap that the strict verifier accepts.

**One byte language for writer and consumer.** The full strict contract
(`_strict_snapshot`) is now shared: `report_from_ledger_bytes` derives the
consumer report through it, and every write REFUSES to extend current bytes
it rejects — duplicate JSON keys, non-finite literals, non-UTF-8, malformed
or non-object rows, hash/chain/envelope forgeries (including the
bool-`sequence` row the chain-level `verify()` cannot see), writer-impossible
rows, numbering or referential violations, and a missing final newline.
Refusals use the established `ledger_integrity_failed:*` family and happen
before mutation, leaving the ledger byte-identical (proven by the
`test_v6r_*_current_ledger_blocks_writer` family).

**Delimiter policy (Option A, strict refusal).** A nonempty ledger whose
final row lost its newline is invalid for both sides
(`ledger_missing_final_newline`); the writer refuses before mutation, so two
JSON objects can never be concatenated onto one line. No operational
trial-registry ledger existed anywhere when this was adopted (read-only scan
of the primary checkout and every linked worktree), so no byte migration was
implied. Prior rows are never rewritten.

**Idempotent replay at capacity.** Dedupe runs inside the lock BEFORE
capacity: replaying an identical registration writes zero bytes and returns
the existing row even when nothing else fits
(`test_v6r_identical_replay_succeeds_even_at_capacity`); a genuinely new
body still refuses with `ledger_capacity_exceeded` and leaves the bytes
identical. The same trial id under a different persisted body remains a
collision/corruption refusal.

**Registration keys.** Every top-level registration key must be an exact
`str` BEFORE set arithmetic, sorting, joining, canonicalization, or
identity; int/bool/tuple keys are the bounded refusal
`registration_key_not_string` at the public boundary — previously a raw
`TypeError` escaped from `sorted()`. The recursive exact JSON-native
parameter validation is unchanged.

`projected_ledger_problem` remains as a conservative unlocked pre-estimate,
but the writer's enforcement point is the locked exact-byte check.

## V7R — request freezing, exact replay, persistent clock, failure-atomic commit (2026-08-02)

The GPT round-7 review proved four more execution-backed gaps in the
transaction surface. Each prior overclaim is corrected here.

**`monotonic_ns` is a persistent logical stamp, NOT a portable raw clock
epoch.** Raw `time.monotonic_ns()` restarts at an arbitrary epoch per
boot/process; a lower-epoch write persisted a decreasing value that poisoned
the whole ledger for every strict reader (`envelope_monotonic_decreasing`)
and every future writer. Both `append()` and `transact()` now derive the
stored value under the exclusive lock as
`max(time.monotonic_ns(), previous_persisted + 1)` via one shared helper
(`_next_persistent_monotonic`) computed from the locked bytes — an exact
non-bool non-negative int, strictly greater than the prior stored value,
never wall-clock dependent. Existing honest ledgers remain readable;
sequence and hash-chain semantics are unchanged.

**Identical replay means the COMPLETE payload, not the outcome label.**
`record_outcome` previously returned the existing row for any same-label
replay — `selected` with different metrics, or `rejected` with a different
reason, silently discarded the caller's payload while reporting success.
Replay equality is now canonical-JSON equality over
`(outcome, metrics, rejection_reason)`: exact match returns the existing
row (zero bytes written); ANY difference is a stable `outcome_conflict`
naming only the outcome labels — metrics and free text are never echoed. A
materially different existing row is never returned as caller success
(proven sequentially and concurrently).

**Caller-owned request objects are NOT assumed immutable.** A caller
mutating a nested parameter or metrics value after validation but before
the lock previously changed the persisted evidence — up to breaking the
content-addressed identity (`ledger_row_trial_id_not_derived`). Public
writers now freeze every mutable request (registration body, outcome
metrics) through the canonical-JSON contract into a detached JSON-native
snapshot, revalidate the snapshot, and use ONLY the snapshot for identity,
dedupe, conflicts, OOS-reuse, and persistence. A mutation racing the
serialization itself is one bounded refusal
(`request_snapshot_unserializable`). Deterministic gate tests mutate nested
parameters, `metric_set`, the acknowledgement list, and outcome metrics
between validation and lock — the persisted row is always the frozen
original.

**`transact()` is failure-atomic; previously a raised transaction could
still have changed the target.** The old direct-append path fsynced AFTER
writing, so an fsync failure raised `OSError` while the (complete, strict-
valid) row was already in the ledger — an ambiguous public failure for a
committed write. `transact()` now commits by complete image: exact old
bytes proven to be the exact prefix of the proposed image, written to a
private same-directory temp file, flushed, file-fsynced, mode set, then
atomically `os.replace`d — the single process-visible commit point.
Injected temp write/flush/fsync and `os.replace` failures all leave the
target byte-identical with no temp residue; a post-commit directory-fsync
failure is never reported as a transaction failure; readers honoring the
sibling lock see only the old or the new complete image. `append()` keeps
its direct write path (compatibility for `shadow_ops`/`token_ledger`)
except for the shared monotonic helper.

## V8R — writer path containment, complete request typing, strict clock (2026-08-02)

GPT round-8 proved three more execution-backed gaps. Each prior overclaim is
corrected.

**Transaction paths are now root-contained; a symlink can no longer redirect
a write outside the repository.** `_assert_contained_path` runs BEFORE any
mkdir, lock open, read, mkstemp, chmod, or replace: the lexical writer path
(and its sibling lock) must sit under the exact repository root, and every
existing component from the root down to the leaf is `lstat`-ed — a symlinked
parent or leaf is rejected by exact type (`ledger_path_symlinked_component` /
`ledger_path_leaf_not_regular_file`), existing parents must be real
directories, and the physically resolved parent must still be beneath the
root. `Path.resolve()` alone is insufficient because an in-root symlink whose
target is also in-root would resolve to a contained path yet still be a
symlink the append-only contract forbids; the lstat walk rejects it. No
outside directory, file, lock, or temp is created on the refusal path (the
check precedes the first `mkdir`). Both the `transact()` and `append()` paths
are covered.

**The COMPLETE request is normalized to exact built-ins before any semantic
operation, not just the body and metrics.** A caller-owned `str` subclass
whose `strip()` or `__eq__` lies previously passed the public writer and only
the raw bytes exposed the forgery (empty note, empty rejection reason,
unknown outcome label, false-matching trial_id). `record_attempt` and
`record_outcome` now freeze every request field — `trial_id`, `note`,
`outcome`, `metrics`, `rejection_reason` — through the canonical-JSON
contract into plain built-ins, validate the trial_id against the existing
64-hex identity shape (`trial_id_invalid`), and run every predicate, closed-
enum membership, replay comparison, dedupe, registration lookup, and
persistence against the frozen values only. No equality, enum test, strip, or
lookup ever touches a caller-owned subclass.

**The persistent logical monotonic stamp is now STRICTLY increasing.** A
correctly hashed, semantically valid ledger with equal adjacent
`monotonic_ns` values previously verified `ok` and the writer would extend
it. Since every writer derives `max(now, previous + 1)`, an equal adjacent
value is as impossible as a decreasing one: the strict envelope gate
(`envelope_monotonic_not_increasing`) and the generic chain verifier
(`clock_rollback`) both reject `<=`. A read-only scan of every operational
`data/runtime/**/*.jsonl` in the primary checkout and all linked worktrees
(186 ledgers) found ZERO with equal or decreasing adjacent stamps, so
tightening the ordering is migration-safe — no operational ledger is
affected.

## V9R — ledger path identity, request normalization, generic envelope (2026-08-02)

GPT round-9 proved four more execution-backed gaps. Each prior overclaim is
corrected; an `lstat`-plus-`flock` pathname walk does NOT bind later
resolution, a regular file does NOT imply a unique ledger identity, JSON
freezing does NOT preserve raw mapping key types, and generic ledgers were
not exactly typed.

**Writer I/O is now fd-relative and hard-link aware.** After the lexical
containment walk, the writer opens a STABLE parent-directory fd by descending
from the repository root with `O_NOFOLLOW` on every component, then runs the
lock, read, temp, and rename entirely relative to that fd — never by
re-resolving `self.path.parent`. The leaf and lock are opened `O_NOFOLLOW`
and `fstat`-checked: a hard-linked leaf (`st_nlink != 1`) is refused
(`ledger_path_hard_linked`) before any write, so an aliased leaf can no
longer reach an outside inode (A), fork the registry identity through
temp+replace (B), or modify a tracked file through the alias (C). A read-only
scan of 186 operational ledgers and locks found none with `st_nlink > 1`, so
the one-link policy is migration-safe. Immediately before the atomic
`os.rename` commit (relative to the held parent fd), the lexical parent must
still `stat` to the held directory (`ledger_parent_identity_changed`), so a
parent rename/symlink swap after the containment walk and lock is refused
rather than committed into either location (D). The `append()` and
`transact()` paths share this contract; a benign `ENOENT`/`ESTALE` from
several writers creating the parent at once is retried, while symlink/
hard-link refusals (which raise `RuntimeError`) are never retried.

**Raw metrics key types are validated before any JSON serialization.**
`canonical_json` (json.dumps) coerces non-string keys — `{1: 2}` → `{"1": 2}`,
`{True: 3}` → `{"true": 3}` — so freezing first silently normalized distinct
Python keys onto one persisted identity and one replay payload. The metrics
container must now be an EXACT built-in `dict` with EXACT `str` keys, checked
before the snapshot; int/bool/float/None/tuple keys are refused
(`metrics_key_not_string`) with no coercion and no collision with the string
spelling (E).

**No caller-overridable mapping/sequence behavior runs before the safe
snapshot boundary.** The registration body, the recursive JSON-native
validator, and the metrics contract now require EXACT built-in `dict`/`list`
by type (not `isinstance`), so a `dict` subclass whose `__iter__`/`items()`
is caller-overridable — and could raise or lie — is refused with a bounded
domain code (`registration_body_not_object` / `metrics_not_object`) instead
of escaping a raw `KeyError` (F).

**The generic envelope is exactly typed.** `AppendOnlyLedger`'s chain
verifier now requires `sequence` and `monotonic_ns` to be EXACT non-negative
`int` (bool forbidden, contiguous sequence, strictly increasing monotonic),
matching the trial-registry strict path. A string, negative, or bool
`monotonic_ns` and a bool `sequence` — which previously reported
`verify().ok=true` and accepted an `append()` — now fail closed and block
writer extension before mutation (G). The persistent logical-stamp helper is
unchanged.

## V10R — temp object, complete write, recursive snapshot, strict generic JSON (2026-08-02)

GPT round-11 proved six more execution-backed gaps. Each prior overclaim is
corrected.

**The transaction temp object is exclusive and unpredictable.** The fixed
`.<leaf>.txn` name could be preplanted (hard link / symlink / FIFO) and then
`O_TRUNC`-opened, writing through the alias. The commit now uses a bounded
unpredictable basename created relative to the held parent fd with
`O_CREAT | O_EXCL | O_NOFOLLOW` (never `O_TRUNC`), fstat-verified a fresh
single-link regular file on the parent's device; a name collision retries a
small bounded number of times, and every pre-commit failure removes only the
transaction-owned temp. A preplanted object at the name is refused, never
reused (A).

**Every byte is written.** A shared `_write_all(fd, data)` loops a memoryview
over `os.write`'s short-write return, retries `InterruptedError`, rejects
negative/oversized counts, and treats a zero-progress return as a bounded
failure — no caller observes success after a partial write. `append()` now
routes through the same failure-atomic complete-image commit as `transact()`,
so a short write can never leave a partial appended row; an incomplete write
leaves the target byte-identical with the temp removed (B).

**The request graph is recursively exact-validated before any inspection.**
`registration_body_problem` iterated nested containers via `isinstance`, so a
`dict`/`list` subclass `parameters`/`metric_set` whose `__iter__` raised
escaped a raw `KeyError`. A recursive `exact_container_problem` now walks the
raw body/metrics dispatching ONLY on exact type — it iterates a container only
after `type() is dict`/`list` proves it is the builtin — and refuses any
dict/list subclass, cycle, or excessive depth with a bounded reason, before
`registration_body_problem` runs (C).

**Parent creation is fd-relative.** The pathname
`self.path.parent.mkdir(parents=True, exist_ok=True)` ran before the fd
descent, so a parent swapped to an outside symlink let the mkdir create
directories outside the repository. The parent is now created (if missing) and
opened by descending from the repository-root fd, creating each missing
component with `os.mkdir(name, dir_fd=…)` and opening it `O_DIRECTORY |
O_NOFOLLOW` — a symlinked component fails `ELOOP` and a created directory is
always inside the repository (D).

**Generic ledger rows use the strict byte language.** `verify()`/`append()`
used the default `json.loads`, so a duplicate object key (last value wins) or a
non-finite literal passed the generic path. One `_strict_json_row` parser —
duplicate-key ordered-pairs hook, non-finite rejection, exact top-level object
— is now shared by `_verify_text` and the write prechecks, so a duplicate-key
current ledger blocks extension before mutation (E).

**One frozen representation binds hash and persistence.** Generic `append`
shallow-copied the caller's record and serialized the mutable graph twice, so a
nested object that changes between the two serializations (e.g. a `list`
subclass whose `__iter__` yields a fresh value) diverged the hashed body from
the persisted row (`row_hash_mismatch`). The record is now recursively
exact-validated and DETACHED into fresh primitives — a subclass/tuple/set/
bytes/non-finite/cycle is a bounded refusal, and the detached graph shares no
object with the caller, so the strict verifier passes immediately after every
writer success (F).

## V11R — generic delimiter, closed envelope, exact snapshot, UTF-8 (2026-08-03)

GPT round-12 proved three deterministic blockers and one public
failure-semantics gap in the GENERIC ledger surface (the trial registry
already enforced most of these on its own path). Each is corrected.

**The final-newline policy is now GENERIC, not registry-only.** A nonempty
generic ledger without a trailing delimiter previously passed `verify()`
(the single row parses) and let `append()` concatenate two JSON objects onto
one line. `_verify_text` now flags `missing_final_newline` on any nonempty
text lacking a trailing `\n`, so `verify()` reports it and the shared
integrity precheck makes `append()`/`transact()` refuse before mutation. A
read-only scan of every operational `data/runtime/**/*.jsonl` (189 ledgers)
found none affected, so the tightening is migration-safe; historical bytes
are never inserted or repaired.

**The generic envelope is a CLOSED, Writer-owned set.** `sequence`,
`prev_row_sha256`, `monotonic_ns`, `row_sha256`, and `dedupe_key` are
Writer-generated; a caller record carrying any of them is refused before
mutation (`record_reserved_field:*`), so a caller can no longer inject a
`row_sha256` that diverges hash from persist, or embed a `dedupe_key` that
survives past the API parameter into a duplicate row. `schema` stays
caller-owned (the committed contract). The public/transaction `dedupe_key`
must be None or an exact built-in `str`, nonempty and bounded
(`dedupe_key_not_string`/`_empty`/`_too_long`) — a str subclass,
whitespace-only string, or non-string is refused before any comparison or
persistence; idempotent exact-string replay is unchanged.

**The registration snapshot rejects EVERY subclass, including scalars.** The
R11 exact-container gate rejected `dict`/`list` subclasses but passed a
hostile `str`/`int`/`float`/`bool` subclass through to the domain predicates
(`.strip()`, equality, set construction), which then invoked its
caller-overridable method and leaked a raw exception. The recursive gate now
also refuses a scalar subclass anywhere in the request graph
(`registration_body_scalar_subclass`), before any predicate runs; exact
scalars pass, and exact tuple/set/bytes/non-finite are still rejected by the
established domain codes.

**Invalid UTF-8 is a bounded integrity problem, not a leaked decoder
traceback.** `verify()` returns `ok=false` with `invalid_utf8`;
`append()`/`transact()` refuse before mutation with the target bytes
unchanged; `rows()`/`find()` keep their lenient-observation role, returning
the factual empty/None view without a `UnicodeDecodeError`. Duplicate-key and
non-finite rejection in strict validation, the exact generic envelope typing,
and lenient `rows()`/`find()` observation of syntactically-readable hostile
rows are all retained.

**Threat-model note (corrected):** these guards fail closed against
hand-edited/corrupt bytes under the cooperative-writer model. An actor with
arbitrary concurrent same-host filesystem write access remains
detected-not-prevented — the standing accepted residual, unchanged.

## V12R — closed persisted-dedupe envelope and a complete parser taxonomy (2026-08-03)

GPT round-13 proved that the R11/R12 dedupe hardening lived only on the API
parameter and that the strict row parser could still leak an interpreter error
out of `verify()`/`append()`/`transact()`. Each gap is closed on the persisted
bytes and the read API, not just the writer entry point.

**The persisted `dedupe_key` obeys the same closed envelope as the API key.**
The API validated its `dedupe_key` argument, but `_verify_text` trusted whatever
`dedupe_key` a persisted row carried and fed it straight into the duplicate
scan. A hand-edited row could therefore carry a non-string
(`null`/`bool`/`int`/`float`/`dict`/`list`), a blank, or an overlong
`dedupe_key` and either be silently accepted or — for an unhashable value —
raise on the set membership test. `_persisted_dedupe_problem` now applies the
exact contract of the API validator (an exact built-in `str`, nonblank,
bounded) to every persisted `dedupe_key` before the duplicate scan, so
`verify()` reports `dedupe_key_invalid:<line>:<reason>` and
`append()`/`transact()` refuse the invalid current ledger with its bytes
unchanged. Only a valid string participates in the duplicate scan, so
`duplicate_dedupe_key` detection for honest keys is preserved exactly.

**The strict row parser has a COMPLETE bounded failure taxonomy.** The former
strict path caught torn JSON but re-ran ordinary `json.loads` on refusal, so a
pathologically nested row raised `RecursionError` and an oversized integer
literal raised `ValueError` (Python's integer-string safety limit) straight out
of `verify()`/`append()`/`transact()`. `_parse_strict_row` now returns
`(row, None)` or `(None, code)` where `code` is one of `duplicate_key`,
`non_finite_literal`, `integer_literal_limit`, `pathological_nesting`,
`non_object_row`, or `torn_or_invalid_json`. The oversized-integer case is
refused by a `parse_int` guard checked against `sys.get_int_max_str_digits()`
*before* `int()` runs — the process-global limit is never disabled or raised.
No `JSONDecodeError`, `ValueError`, `RecursionError`, or `TypeError` escapes, so
no caller re-runs a bare `json.loads` and leaks one. `verify()` keeps the pinned
`torn_or_truncated_row:<line>` token for torn JSON and reports every other
refusal as `non_strict_row:<line>:<code>`. `rows()`/`find()` keep their lenient
observation role: they stop at the first unreadable row and return the factual
partial view, never a traceback.

**`find()` validates its lookup key before any equality.** A lookup key now
passes a strict string contract — an exact built-in `str`, nonblank and bounded
— *before* the file is read or any comparison runs, so a hostile object or `str`
subclass whose `__eq__` raises can no longer leak a traceback, and an
always-equal `str` subclass can no longer false-match an unrelated stored row.
Unlike `append()`/`transact()` — where `dedupe_key=None` means "no dedupe key" —
a `None` **lookup** is forbidden: it would otherwise false-match a stored row
that carries no `dedupe_key` (`row.get("dedupe_key") == None`). The two entry
points share the string core of the contract (`_require_dedupe_key_string`)
while keeping append's `None` allowance explicit and the lookup's `None` refusal
explicit. A genuine exact-string lookup still resolves a present key and reports
an absent one as `None`.

Duplicate-key and non-finite rejection in strict validation, the exact generic
envelope typing, the generic final-newline contract, and lenient `rows()`/
`find()` observation of syntactically-readable hostile rows are all retained.
The cooperative-writer threat model is unchanged: an actor with arbitrary
concurrent same-host filesystem write access remains detected-not-prevented.
