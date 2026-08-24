# Research-ledger corrections (append-only)

`data/research_ledger.json` is append-only. Its header has always promised that "corrections
append with their own timestamp", but until 2026-08-04 there was no schema for one — so a ledger
row pointing at a report that no longer existed had nowhere to be resolved and simply accumulated.

Two such rows were found by `scripts/control_plane_reconcile.py`:

| target session | referenced report | status |
|---|---|---|
| 2026-07-13 | `reports/morning/preliminary/2026-07-13_preliminary_live_price_research_incomplete_0607PT.md` | absent from working tree, git history and `data/MANIFEST.json` |
| 2026-07-29 | `reports/morning/preliminary/2026-07-29_preliminary_catalyst_unverified_0608PT.md` | absent from working tree, git history and `data/MANIFEST.json` |

Both rows are `PRELIMINARY_*`: non-official, `buy_authorized=false`, granting no Buy or execution
authority. Their absence is an evidence-completeness defect, not an authority defect.

## What a correction is, and is not

A correction is a **new record appended alongside** the entries. It never touches the entry it
annotates.

* It **never edits or deletes** the original ledger entry. The row remains the authoritative
  record that the freeze occurred, with its original `report_path` and `report_sha256` intact.
* It **never reconstructs, regenerates, or back-dates** the missing report. A report that does
  not exist stays non-existent; inventing one would be fabricated evidence.
* It **records what was observed**: the finding, the timestamp, and the concrete evidence lines
  (working tree, git history, manifest coverage).

The audit trail is strictly richer after a correction than before it.

## Schema — `research_ledger_correction/1.0`

Stored under a top-level `corrections` array. Validated by
`scripts/research_metrics.validate_correction()`; `append_correction()` refuses anything invalid.

| field | meaning |
|---|---|
| `schema` | `research_ledger_correction/1.0` |
| `correction_type` | `missing_report_artifact` |
| `target_session_date` | the annotated entry's session |
| `original_report_path` | the path exactly as the entry records it — **preserved, never rewritten** |
| `observed_utc` | when the absence was observed |
| `finding` | one-sentence statement of the defect |
| `evidence` | non-empty list of concrete observations |
| `resolution` | `acknowledged_absent_never_reconstructed` |
| `original_entry_preserved` | must be `true` |
| `report_reconstructed` | must be `false` |

The last two are asserted **inside** the record so a reader never has to trust the writer's
intent: a record claiming it rewrote an entry, or that it rebuilt the report, is rejected.

## Reconciliation

`scripts/research_metrics.dangling_report_references()` splits dangling rows into `unresolved`
and `acknowledged`, and `scripts/control_plane_reconcile.py` reports the invariant as satisfied
only when `unresolved` is empty. Acknowledged rows stay visible in `measured.acknowledged`
forever — they are resolved, not erased.

An **invalid** correction acknowledges nothing, so a malformed record can never silence a real
dangling reference.

## Migration

No migration is required for existing ledgers. `corrections` is optional and additive:

* readers that predate it are unaffected — `entries`, `schema` and `_comment` are untouched;
* `research_metrics.load()`/`_load()` validation is unchanged (schema + `entries` list);
* a ledger with no `corrections` key behaves exactly as before, and every dangling row is
  reported as `unresolved`.

To record a new correction:

```bash
cd /Users/prince/Projects/trading-system
python3 - <<'PY'
import sys; sys.path.insert(0, "scripts")
import research_metrics as rm
print(rm.dangling_report_references())      # find what is unresolved
PY
```

then append one validated record per unresolved row via `rm.append_correction(record)`. Recording
a correction is a §12 change: note it in `journal/system_changes.md`.

Tests: `scripts/test_research_metrics.py::TestAppendOnlyCorrections`, including a live assertion
that the shipped ledger has **zero unresolved** dangling references.
