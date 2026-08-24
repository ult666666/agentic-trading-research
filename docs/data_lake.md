# Point-in-Time Data Lake

Status: Phase 1A foundation and the WP-09 point-in-time adjustment contract are implemented. The
05:00 PT LaunchAgent was loaded on 2026-07-09. Official-source adapters, immutable ingestion,
cutoff snapshots, quality/readiness gates, security-master interval checks, quant export, and the
supervised runner are operational. The public BLS canary ingested 59 normalized records into one
validated immutable batch/snapshot. A disabled, shadow-only 30-session provider bake-off evaluator
is also wired into intake. No commercial data has been collected and a broad licensed ten-year
lake is not yet present, so `research_ready=false` remains expected.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document. This layer collects and attests
research data only. It cannot classify a candidate, activate a strategy, change risk limits, grant
Buy authority, preview an order, or execute anything.

## Architecture

```text
05:00 PT LaunchAgent
        |
        v
autonomy_orchestrator.py
  trading day -> manifest -> strict broker deny-list -> time window
        |
        v
run_data_intake.sh (no model)
        |
        +-> SEC EDGAR adapter (only with private SEC_USER_AGENT)
        +-> BLS public adapter
        +-> Nasdaq Trader current-directory adapter
        +-> Federal Reserve H.15 current-published adapter
        +-> CFTC COT weekly-context adapter
        +-> existing point_in_time_bars adapter
        +-> commercial bake-off adapters: disabled
        |
        v
provider_collection_batch/1.0
        |
        v
immutable JSONL partition + data_lake_batch_manifest/1.0
        |
        +-> data_lake_snapshot/1.0 (knowledge-cutoff bound)
        +-> data_lake_quality/1.0
        +-> data_lake_readiness/1.0
        +-> free_source_health/1.0 (cross-session aggregate observability)
        +-> local deduplicated alert transition (fixed wrapper notification)
        +-> provider bake-off daily receipt + rollup (aggregate shadow diagnostics only)
        |
        +-> adjustment policy + corporate-action contract
        |     -> raw | split_only | total_return derived bar view
        |
        v
optional quant_bar_snapshot/1.0 or adjusted_bar_view/1.0
        -> existing offline quant.features validator (never auto-consumed)
```

## Files

- [`config/data_lake.json`](../config/data_lake.json): exact provider, dataset, retention,
  readiness, and safety contract.
- [`scripts/provider_adapters/base.py`](../scripts/provider_adapters/base.py): normalized record
  and collection-batch schemas.
- [`scripts/provider_adapters/sec_edgar.py`](../scripts/provider_adapters/sec_edgar.py): SEC
  submissions parser and bounded public API collector.
- [`scripts/provider_adapters/bls_public.py`](../scripts/provider_adapters/bls_public.py): BLS
  time-series parser and public collector.
- [`scripts/provider_adapters/bls_release_calendar.py`](../scripts/provider_adapters/bls_release_calendar.py):
  official BLS ICS schedule collector; scheduled times never become actual publication claims.
- [`scripts/provider_adapters/nasdaq_trader_public.py`](../scripts/provider_adapters/nasdaq_trader_public.py):
  current symbol-directory reference collector; not historical delisting coverage.
- [`scripts/provider_adapters/nasdaq_listing_events.py`](../scripts/provider_adapters/nasdaq_listing_events.py):
  current Nasdaq system add/delete event collector; accumulated observations only.
- [`scripts/provider_adapters/nasdaq_ex_date_notices.py`](../scripts/provider_adapters/nasdaq_ex_date_notices.py):
  special ex-date notice collector; never a complete corporate-action record.
- [`scripts/provider_adapters/fed_h15_public.py`](../scripts/provider_adapters/fed_h15_public.py):
  current-published H.15 rate context without vintage claims.
- [`scripts/provider_adapters/cftc_cot_public.py`](../scripts/provider_adapters/cftc_cot_public.py):
  weekly futures-only positioning context; never a permitted instrument or standalone signal.
- [`scripts/provider_adapters/local_bars.py`](../scripts/provider_adapters/local_bars.py): migration
  adapter for the existing point-in-time bar archive.
- [`scripts/data_lake_ingest.py`](../scripts/data_lake_ingest.py): atomic immutable writer and
  manifest validator.
- [`scripts/data_lake_snapshot.py`](../scripts/data_lake_snapshot.py): cutoff snapshots and quant
  bar export, including explicit policy-derived views.
- [`scripts/data_lake_quality.py`](../scripts/data_lake_quality.py): structural and research-
  readiness assessment.
- [`config/adjustment_policy.json`](../config/adjustment_policy.json): versioned raw, split-only,
  total-return, cutoff, revision, and safety semantics.
- [`scripts/adjustment_policy.py`](../scripts/adjustment_policy.py): deterministic corporate-action
  validation, factor materialization, lineage, round-trip checks, and rebuild verification. See
  [`docs/adjustment_policy.md`](adjustment_policy.md).
- [`scripts/universe_master.py`](../scripts/universe_master.py): ticker/listing interval and
  delisting-preserving security master.
- [`scripts/data_lake_intake.py`](../scripts/data_lake_intake.py): one deterministic intake run.
- [`scripts/security_master_rollforward.py`](../scripts/security_master_rollforward.py): shadow
  listing-episode rollforward with explicit unknown starts and conflicts; see
  [`docs/security_master_rollforward.md`](security_master_rollforward.md).
- [`scripts/provider_bakeoff.py`](../scripts/provider_bakeoff.py): disabled-provider daily quality
  evaluator and 30-session scorecard; see [`docs/provider_bakeoff.md`](provider_bakeoff.md).
- [`scripts/free_source_health.py`](../scripts/free_source_health.py): validates hash-linked intake
  receipts and redacted source plans, then derives provider/lane reliability without raw payloads;
  see [`docs/free_source_health.md`](free_source_health.md).

Runtime storage is untracked:

```text
data/runtime/data_lake/
  lake/<dataset>/observed_date=<date>/provider=<provider>/batch=<sha256>/
    records.jsonl
    manifest.json
  snapshots/snapshot_date=<date>/snapshot=<sha256>.json
  readiness/<target>.json
```

## Time Contract

Every normalized record keeps these clocks separate:

- `event_time_utc`: when the underlying event happened, if known;
- `publication_time_utc`: when the source published/accepted it, if known;
- `effective_from_utc` and `effective_to_utc`: the interval the record describes;
- `observed_at_utc`: when this system first captured this vintage.

Unknown times remain `null`. A date-only filing never becomes a fabricated intraday timestamp.
BLS observations currently have `availability_precision=retrieval_only` because the time-series
response does not carry a per-observation release timestamp; those records are retained but cannot
enter release-time event studies. The separate BLS calendar supplies scheduled ET/UTC events and
release-family mappings, but deliberately does not mutate those observation timestamps: a schedule
is not proof that publication actually occurred. See
[`docs/bls_release_calendar.md`](bls_release_calendar.md).

## Immutability and Reproducibility

Each adapter returns a hash-validated `provider_collection_batch/1.0`. Ingestion writes one new
directory atomically. The identity combines provider, dataset, normalized records, and semantic
source-payload hash:

- identical retries resolve to `idempotent_existing`;
- changed vintages create new content-addressed batches;
- an existing directory with inconsistent bytes is an immutable collision and is never replaced;
- records, batch metadata, records file, and manifest each have independently recomputable hashes;
- snapshots contain only manifests observed at or before their knowledge cutoff.

Raw API responses are hashed and discarded. No key, User-Agent value, account data, broker payload,
or raw licensed payload is stored.

## Providers

| Adapter | Default | Role | Important limit |
|---|---:|---|---|
| SEC EDGAR | enabled, conditional | filings | Skips when private `SEC_USER_AGENT` is absent |
| BLS public | enabled | macro observations | Retrieval-only vintage until release calendar is joined |
| BLS release calendar | enabled | official scheduled macro event risk | Schedule only; actual release remains unconfirmed |
| Nasdaq Trader public | enabled | current security identity | Point snapshot; no survivorship-free or delisted history |
| Nasdaq listing events | enabled | add/delete event observations | Current event file; shadow accumulation only |
| Nasdaq ex-date notices | enabled | corporate-action follow-up leads | Exception notice only; no type, amount, or adjustment |
| Federal Reserve H.15 | enabled | current-published rate context | No revision vintage or release-time authority |
| CFTC COT public | enabled | weekly cross-asset regime context | Lagged context only; futures remain prohibited |
| Local PIT bars | enabled | daily-bar migration | Empty until post-close accumulation produces bars |
| Massive bake-off | disabled | shadow evaluation candidate | Harness ready; adapter, license, credential, and collection approval absent |
| Databento bake-off | disabled | shadow evaluation candidate | Harness ready; adapter, license, credential, and collection approval absent |

The capability matrix remains bounded by
[`config/provider_capabilities.yaml`](../config/provider_capabilities.yaml) and the enforced
[`config/source_authority.yaml`](../config/source_authority.yaml). `authority_enabled=false` is
mandatory for every lake adapter. A subscription or successful collection never grants production
authority.

SEC's official API documentation states that data APIs require no authentication, submissions are
updated throughout the day, and bulk archives are preferable for broad retrieval:
https://www.sec.gov/search-filings/edgar-application-programming-interfaces. SEC fair-access
guidance limits automated users to no more than 10 requests per second and requires a declared
User-Agent: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data. This
adapter is intentionally slower and retrieves only configured CIKs.

BLS documents the public time-series endpoint and query limits at
https://www.bls.gov/developers/ and https://www.bls.gov/developers/api_faqs.htm. The intake uses
the unregistered single-series read path and remains well below the published daily/rate limits.

## Readiness Semantics

`quality_gate_pass` and `research_ready` are deliberately different:

- `quality_gate_pass=true`: stored files are structurally valid, hash-bound, nonfuture, and free
  of same-vintage value conflicts.
- `research_ready=true`: structural quality passes and all required datasets/coverage exist.

Current readiness additionally requires at least 3,000 security-master symbols, 1,000 daily-bar
symbols, security-master/daily-bar/corporate-action history reaching 2016-01-01, delisted coverage,
both split and cash-dividend actions, and explicit adjustment semantics. The quality receipt reports
these observed counts, dates, and action types under `historical_coverage_acceptance`. A current
directory or one-symbol fixture cannot pass by merely making the datasets nonempty. The quality
receipt also validates the adjustment policy and every corporate-action payload it can see, and
reports whether a derived view can be built. Missing requirements produce a valid `degraded`
receipt. They do not become zero values and cannot be overridden by a model.
`adjustment_view_ready=true` still does not enable any automatic quant or production consumer.

Intake terminal states:

- `ready`: structural and research-readiness requirements pass;
- `degraded`: receipt is valid, but data/coverage/provider prerequisites are incomplete;
- `blocked`: config, authority matrix, immutable write, snapshot, or structural quality failed;
- `dry_run`: all local contracts passed and network/model calls were suppressed.

## Commands

Validate the configuration and existing lake:

```bash
python3 -c "import sys; sys.path.insert(0,'scripts'); import data_lake_ingest as d; print(d.load_config()[1])"
python3 scripts/data_lake_ingest.py scan
python3 scripts/data_lake_quality.py
python3 scripts/security_master_rollforward.py --check
python3 scripts/free_source_health.py --check
```

Attended no-network intake canary:

```bash
python3 scripts/data_lake_intake.py --target YYYY-MM-DD --dry-run --no-network
```

Create and validate a cutoff snapshot:

```bash
python3 scripts/data_lake_snapshot.py create --cutoff YYYY-MM-DDTHH:MM:SS+00:00
python3 scripts/data_lake_snapshot.py validate --snapshot <snapshot-path>
```

Export one unadjusted provider series for the existing offline feature builder:

```bash
python3 scripts/data_lake_snapshot.py export-bars \
  --snapshot <snapshot-path> --adjustment unadjusted \
  --source-provider <provider> --out <bars.json>
```

Derive a policy-bound split-only or total-return view:

```bash
python3 scripts/adjustment_policy.py check
python3 scripts/data_lake_snapshot.py derive-bars \
  --snapshot <snapshot-path> --lake-root data/runtime/data_lake/lake \
  --view split_only --source-provider <provider> --out <adjusted-bars.json>
python3 scripts/adjustment_policy.py verify \
  --artifact <adjusted-bars.json> --snapshot <snapshot-path> \
  --lake-root data/runtime/data_lake/lake
```

The derived artifact binds the source snapshot, cutoff, provider, policy version/hash, raw-record
lineage, corporate-action revision chain, factors, round-trip checks, and output hashes. Provider-
adjusted bars are not accepted as policy `raw` input. See
[`docs/adjustment_policy.md`](adjustment_policy.md) for exact math and correction semantics.

The feature builder now accepts a true pre-event archive containing only bars strictly before the
event session. It no longer requires the future event-day closing bar merely to validate alignment;
the versioned market calendar validates that the event date is a trading session.

## Private SEC Identity

`SEC_USER_AGENT` and its explicit `SEC_USER_AGENT_APPROVED=1` transmission-consent marker are
allowlisted private metadata in `scripts/lib/secrets_loader.py`. Configure them only through
`scripts/setup_sec_user_agent.sh`, which writes the gitignored `secrets/trading.env` atomically with
owner-only permissions and performs no network request. The intake runner writes a redacted
readiness receipt, loads the values only for the bounded collector, and unsets them before
terminal-state handling. Missing or invalid identity blocks only SEC collection; no artifact stores
the identity, email, or an identity hash. See [`docs/sec_access.md`](sec_access.md).

## Remaining Phase 1 Work

This foundation does not satisfy the roadmap's dataset acceptance gate. Remaining work requires:

1. approved license attestations plus disabled shadow adapters to supply the 30-session bake-off;
2. a separately reviewed historical security-master seed and delisted history; the daily Nasdaq
   add/delete rollforward now accumulates future observations but cannot reconstruct unknown past;
3. licensed split/dividend coverage plus merger/ticker-change reconciliation (the local split and
   cash-dividend policy is implemented, but real coverage is absent);
4. broad adjusted and unadjusted daily history validated against the local policy;
5. focused one-minute/NBBO event windows;
6. point-in-time earnings consensus, guidance, analyst actions, and publication-timestamped news;
7. a Parquet/DuckDB migration decision after the JSONL pilot validates schema and retention needs.

Until those are complete, no strategy can claim broad point-in-time validation or demonstrated
profitability.
