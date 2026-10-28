# Point-in-Time Adjustment Policy

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: version 1.0.0 is implemented for offline research. It derives reproducible `raw`,
`split_only`, and `total_return` daily-bar views from one validated data-lake snapshot. It does
not activate a provider, feed production scoring, change a strategy, grant Buy authority, or
access a broker.

Authority: `AGENTS.md` overrides this document. The machine-enforced contract is
`config/adjustment_policy.json`, implemented by
[`scripts/adjustment_policy.py`](../scripts/adjustment_policy.py).

## Why This Exists

A backtest can leak future information when today's corrected split or dividend history is used
as if it had been known at an older research cutoff. It can also silently mix vendor-adjusted and
locally adjusted prices. This layer makes the knowledge cutoff, source provider, action revision,
math, lineage, and policy version part of each derived dataset's identity.

The raw lake remains immutable. Every adjusted view is a derived artifact that can be rebuilt and
byte-compared against its source snapshot.

## Views

| View | Price treatment | Volume treatment | Corporate actions required |
|---|---|---|---|
| `raw` | Preserve provider unadjusted OHLC values exactly | Preserve provider volume exactly | No |
| `split_only` | Apply effective split factors to pre-effective bars | Apply inverse price factor | Yes |
| `total_return` | Apply split factors and cash-dividend factors to pre-effective bars | Split adjustment only | Yes |

The materializer explicitly requests provider records marked `adjusted=false`. Provider-adjusted
bars cannot be relabeled as a policy `raw` view.

For a split where `ratio = new shares / old shares`:

```text
pre-effective adjusted price  = raw price / ratio
pre-effective adjusted volume = raw volume * ratio
```

For a cash dividend with amount `cash` and the latest unadjusted close before the ex-date
`previous_close`:

```text
pre-ex-date price factor = (previous_close - cash) / previous_close
```

All factors use decimal arithmetic. Derived values are rounded to 10 decimal places using
round-half-even. Each output bar is reverse-checked against its raw value within the configured
rounding tolerance; any failed round trip blocks the artifact.

## Point-in-Time Rules

1. The input snapshot must pass `data_lake_snapshot.validate_snapshot`.
2. Every raw bar and corporate-action record must have been observed at or before the snapshot's
   `knowledge_cutoff_utc`.
3. A bar dated after the cutoff date is rejected.
4. An action known by the cutoff but effective later is retained in lineage as
   `known_future_not_effective` and is not applied.
5. A correction first observed after the cutoff cannot change an older snapshot.
6. Unknown or unsupported action types fail closed.

## Revision Contract

Each action has a stable `action_id` and a linear revision chain:

- revision 1 is `active` and has no parent;
- later revisions are contiguous and are `corrected` or `rescinded`;
- each revision names the exact SHA-256 of its predecessor;
- action type, symbol, security ID, provider, and action ID cannot change in a chain;
- observation times must strictly increase;
- no revision may follow a rescind;
- the latest visible correction wins, while a visible rescind removes the action.

A duplicate revision, gap, branch, wrong parent, identity mutation, non-increasing timestamp, or
post-rescind revision blocks the entire derived view.

## Artifact Contract

An `adjusted_bar_view/1.0` artifact binds:

- policy version and policy SHA-256;
- source snapshot ID, snapshot SHA-256, and knowledge cutoff;
- explicit source provider and view name;
- every raw bar record hash and applied action record hash;
- factors and per-bar round-trip result;
- action states, revisions, counts, and known-future exclusions;
- bar payload hash, lineage hash, dataset ID, and whole-view hash;
- compatibility errors from the existing offline `quant.features` bar validator;
- immutable safety fields stating zero production, strategy, Buy, broker, or execution authority.

`quant_compatible=true` means only that the bar shape passes the offline feature validator. No
quant factory, morning workflow, scorer, or report consumes these views automatically.

## Commands

Validate the shipped policy:

```bash
python3 scripts/adjustment_policy.py check
```

Derive one immutable view from an existing snapshot:

```bash
python3 scripts/data_lake_snapshot.py derive-bars \
  --snapshot <snapshot.json> \
  --lake-root data/runtime/data_lake/lake \
  --view split_only \
  --source-provider <provider> \
  --out <new-artifact.json>
```

Validate and independently rebuild an artifact:

```bash
python3 scripts/adjustment_policy.py validate --artifact <artifact.json>
python3 scripts/adjustment_policy.py verify \
  --artifact <artifact.json> \
  --snapshot <snapshot.json> \
  --lake-root data/runtime/data_lake/lake
```

Output creation is atomic and refuses to overwrite an existing path.

## Source Semantics

The schema follows three primary-source principles:

- Databento's corporate-actions schema separates record time, action identity, action state, and
  event dates: https://databento.com/docs/schemas-and-data-formats/corporate-actions
- Databento's symbology standard preserves historical identifiers rather than assuming today's
  symbol existed at every historical date: https://databento.com/docs/standards-and-conventions/symbology
- Massive distinguishes adjusted REST aggregates from unadjusted stock flat files and publishes
  split factors separately: https://massive.com/docs/rest/stocks and
  https://massive.com/docs/flat-files/stocks/overview

The SEC's investor explanation confirms that a stock split changes shares and per-share price but
does not itself change aggregate shareholder value: https://www.sec.gov/answers/stocksplit.htm

These references inform the local contract; they do not authorize either provider or establish
that the current lake has complete corporate-action coverage.

## Current Honest State

The policy, schemas, materializer, lake-quality integration, and adversarial tests are implemented.
The repository does not yet contain an approved commercial corporate-action feed or broad licensed
history. Therefore the real lake is still expected to report missing corporate-action coverage,
and no claim of historical edge or profitability is supported by this work alone.
