# Release Snapshot Runbook

Status: `release_snapshot/1.0` implemented 2026-07-10 as the non-destructive prerequisite for
clean release work. A snapshot is not a commit, branch, approval, or production deployment.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## What It Captures

[`scripts/release_snapshot.py`](../scripts/release_snapshot.py) reads one Git worktree and writes a
private bundle outside it:

```text
base.bundle                 committed HEAD graph
staged.patch                HEAD -> index, binary/full-index patch
unstaged.patch              index -> worktree, binary/full-index patch
status.porcelain-v2.z       exact NUL-delimited status
untracked.tar               deterministic non-ignored regular-file archive
receipt.json                hashes, modes, inventory, safety contract
```

The default destination is
`~/.local/share/trading-system/release_snapshots/`, mode `0700`; artifacts are `0600`. Ignored
files are never enumerated, so `.env`, `secrets/`, tokens, runtime state, and logs are excluded by
the repository ignore rules.

`scripts/build_manifest.py` uses the same Git-authoritative tracked plus untracked/nonignored
inventory boundary. Machine-local ignored files such as `.claude/settings.local.json` are not
release inputs and cannot make a clean restore fail its manifest check; their live safety effect is
validated separately by the strict broker-permission validator when they are present.

Capture reads HEAD, both patches, NUL status, and every untracked hash twice. A concurrent change
deletes the partial snapshot and fails. The source index/worktree receives no write, stage, commit,
checkout, reset, clean, stash, or branch operation.

## Create And Verify

```bash
python3 scripts/release_snapshot.py create

python3 scripts/release_snapshot.py verify \
  --snapshot ~/.local/share/trading-system/release_snapshots/<snapshot-id>
```

After capture, perform the exact forensic comparison and write its diagnostic receipt:

```bash
python3 scripts/release_snapshot.py verify-current \
  --snapshot ~/.local/share/trading-system/release_snapshots/<snapshot-id> \
  --out data/runtime/release_provenance/current.json
```

This command deliberately answers only whether the complete Git-visible state still matches the
snapshot byte-for-byte. Any subsequent difference keeps `status=drift` and
`release_reproducible=false`. That exact result must remain visible for forensics; consumers must
not relabel it merely because a difference is operationally safe.

Runtime admission is a separate read-only decision:

```bash
python3 scripts/runtime_provenance.py --json
```

`runtime_provenance.py` preserves the exact snapshot result and computes
`runtime_binding_status`. Only `match` and validator-approved `expected_output_drift` pass. The
second status is limited to recognized non-executable create-once report paths, strict HEAD-prefix
journal appends, and receipt-bound rewrites whose owning contract requires a receipt (currently the
post-close watchlist). Report authority is still revalidated by its own consumers; this source gate
does not grant Official, Buy, or execution authority. It also does not make the snapshot
reproducible. Staged changes, control/critical-source drift, deletions or rewrites, append-prefix
violations, unknown output paths, malformed provenance, and every unapproved difference remain
blocked.

After an intended source tranche is reviewed, committed, manifest-checked, and clean, take a new
snapshot as part of settlement. Do not refresh a release snapshot merely to hide expected generated
outputs or silence a gate: safe output drift is supposed to remain in the forensic record. The
snapshot receipts expose only a hash of the private snapshot location.

## Restore Drill

Restore accepts only a new or empty destination. It initializes a separate repository, fetches the
bundle, applies the staged patch to the index, applies the unstaged patch to the worktree, restores
untracked files without `extractall`, and re-hashes the result.

```bash
python3 scripts/release_snapshot.py restore \
  --snapshot ~/.local/share/trading-system/release_snapshots/<snapshot-id> \
  --destination /private/tmp/trading-system-restore-drill
```

The source repository and snapshot remain untouched. A failed restore directory is retained for
forensics and must be removed manually only after inspection.

## Refusals And Limits

- submodules and untracked symlinks are refused in version 1.0;
- non-regular untracked files are refused;
- one untracked file is limited to 64 MiB and the total to 256 MiB;
- archive path traversal, duplicate members, content/mode drift, torn artifacts, and hash mismatch
  fail closed;
- the snapshot root must be outside the source repository;
- ignored files cannot be restored because they are deliberately excluded.

After a verified snapshot and restore drill, reversible commits can be prepared in a separate clean
worktree/repository. Commit selection remains an explicit human release decision; this tool never
stages or commits the current worktree.
