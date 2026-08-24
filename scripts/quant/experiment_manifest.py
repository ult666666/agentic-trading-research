#!/usr/bin/env python3
"""Offline quant lab — EXPERIMENT MANIFEST (Layer 3 skeleton). DIAGNOSTIC ONLY.

Safety contract:
  * Research lab bookkeeping. A manifest records what an offline experiment TRIED —
    including every rejected variant and the REQUIRED total trial count — so
    multiple-testing can be accounted for honestly. It never activates anything:
    disclaimers pin diagnostic_only=true, feeds_production=false, and
    requires_category_D_approval_before_activation=true, and every output (including
    refusals) self-labels with quant.DIAGNOSTIC_FIELDS.
  * Fail-closed: missing snapshot file, malformed parameters, missing/invalid trial
    count, or a trial count smaller than the reported variants => {"error": ...} +
    exit 1. A manifest that fails its own validation is never written.
  * Append-only spirit: an existing manifest is NEVER overwritten (no history rewrite).
  * git commit capture is read-only and fail-soft: unavailable => null, never fabricated.
  * Atomic writes (tempfile + os.replace). std-lib only.

Layout: <root>/<experiment_id>/manifest.json, default root data/runtime/experiments/.

Run:
  python3 scripts/quant/experiment_manifest.py create --id exp01 \
      --data-snapshot data/replay_bars.json --param-grid '{"lookback": [10, 20]}' \
      --trials 8 --selected '[{"lookback": 10}]' --rejected '[{"lookback": 20}]'
  python3 scripts/quant/experiment_manifest.py validate --path <manifest.json>
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import DIAGNOSTIC_FIELDS  # noqa: E402

TOOL = "quant.experiment_manifest"
DEFAULT_ROOT = ROOT / "data" / "runtime" / "experiments"
MANIFEST_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_DISCLAIMERS = {
    "diagnostic_only": True,
    "feeds_production": False,
    "requires_category_D_approval_before_activation": True,
}


def _base() -> dict:
    return {**DIAGNOSTIC_FIELDS, "tool": TOOL}


def _refuse(err, **extra):
    return {**_base(), "error": err, **extra}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit_or_none():
    """Read-only, fail-soft: current HEAD commit, or None when unavailable (no repo,
    no git binary, timeout). NEVER fabricated, never an exception."""
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    sha = proc.stdout.strip().lower()
    return sha if proc.returncode == 0 and _COMMIT_RE.match(sha) else None


def validate_manifest(m) -> list:
    """Return a list of error strings (empty == valid). Fail-closed on every field."""
    if not isinstance(m, dict):
        return ["manifest_not_an_object"]
    errors = []
    for k, v in DIAGNOSTIC_FIELDS.items():
        if m.get(k) != v:
            errors.append(f"diagnostic_field_mismatch:{k}")
    if not isinstance(m.get("experiment_id"), str) or not _ID_RE.match(m.get("experiment_id") or ""):
        errors.append("invalid_experiment_id")
    if not isinstance(m.get("created_utc"), str):
        errors.append("missing_created_utc")
    if "git_commit" not in m:
        errors.append("missing_git_commit_key")
    elif m["git_commit"] is not None and (
            not isinstance(m["git_commit"], str) or not _COMMIT_RE.match(m["git_commit"])):
        errors.append("invalid_git_commit")
    snaps = m.get("data_snapshots")
    if not isinstance(snaps, list) or not snaps:
        errors.append("missing_data_snapshots")
    else:
        for i, s in enumerate(snaps):
            if (not isinstance(s, dict) or not isinstance(s.get("path"), str)
                    or not isinstance(s.get("sha256"), str)
                    or not _SHA_RE.match(s.get("sha256") or "")):
                errors.append(f"invalid_data_snapshot:{i}")
    if not isinstance(m.get("parameter_grid"), dict):
        errors.append("invalid_parameter_grid")
    # REQUIRED number-of-trials: without it, multiple-testing cannot be accounted for.
    if "n_trials" not in m:
        errors.append("missing_n_trials")
    elif (not isinstance(m["n_trials"], int) or isinstance(m["n_trials"], bool)
          or m["n_trials"] < 1):
        errors.append("invalid_n_trials")
    sel, rej = m.get("selected_variants"), m.get("rejected_variants")
    if not isinstance(sel, list):
        errors.append("invalid_selected_variants")
    if not isinstance(rej, list):
        errors.append("invalid_rejected_variants")
    if (isinstance(sel, list) and isinstance(rej, list) and isinstance(m.get("n_trials"), int)
            and not isinstance(m.get("n_trials"), bool)
            and m["n_trials"] < len(sel) + len(rej)):
        errors.append("n_trials_less_than_reported_variants")
    disc = m.get("disclaimers")
    if not isinstance(disc, dict):
        errors.append("missing_disclaimers")
    else:
        for k, v in REQUIRED_DISCLAIMERS.items():
            if disc.get(k) is not v:
                errors.append(f"disclaimer_incorrect:{k}")
    return errors


def build_manifest(experiment_id, snapshot_paths, parameter_grid, n_trials,
                   selected, rejected, note=None):
    """Assemble (but do not write) a manifest dict. Refusals carry "error"."""
    if not isinstance(experiment_id, str) or not _ID_RE.match(experiment_id):
        return _refuse("invalid_experiment_id", experiment_id=experiment_id)
    if not isinstance(snapshot_paths, (list, tuple)) or not snapshot_paths:
        return _refuse("no_data_snapshots")
    snaps = []
    for p in snapshot_paths:
        path = Path(p)
        if not path.is_file():
            return _refuse("snapshot_missing", snapshot_path=str(p))
        try:
            digest = _sha256_file(path)
        except OSError:
            return _refuse("snapshot_unreadable", snapshot_path=str(p))
        snaps.append({"path": str(p), "sha256": digest})
    if not isinstance(parameter_grid, dict):
        return _refuse("invalid_parameter_grid")
    if not isinstance(n_trials, int) or isinstance(n_trials, bool) or n_trials < 1:
        return _refuse("invalid_n_trials", value=n_trials)
    if not isinstance(selected, list) or not isinstance(rejected, list):
        return _refuse("invalid_variants")
    if n_trials < len(selected) + len(rejected):
        return _refuse("n_trials_less_than_reported_variants", n_trials=n_trials,
                       reported_variants=len(selected) + len(rejected))

    manifest = {
        **_base(),
        "manifest_version": MANIFEST_VERSION,
        "experiment_id": experiment_id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_commit": git_commit_or_none(),
        "data_snapshots": snaps,
        "parameter_grid": parameter_grid,
        "n_trials": n_trials,
        "multiple_testing_note": ("n_trials is the TOTAL number of variant evaluations "
                                  "run in this experiment (including abandoned ones); "
                                  "any selection must be judged against all trials."),
        "selected_variants": selected,
        "rejected_variants": rejected,
        "disclaimers": dict(REQUIRED_DISCLAIMERS),
    }
    if note is not None:
        manifest["operator_note"] = str(note)
    errors = validate_manifest(manifest)
    if errors:  # fail-closed second opinion; unreachable by construction
        return _refuse("manifest_failed_self_validation", validation_errors=errors)
    return manifest


def create_manifest(experiment_id, root, snapshot_paths, parameter_grid, n_trials,
                    selected, rejected, note=None):
    """Build, self-validate, and atomically write <root>/<id>/manifest.json.
    Never overwrites an existing manifest."""
    manifest = build_manifest(experiment_id, snapshot_paths, parameter_grid, n_trials,
                              selected, rejected, note=note)
    if "error" in manifest:
        return manifest
    target_dir = Path(root) / experiment_id
    manifest_path = target_dir / "manifest.json"
    if manifest_path.exists():
        return _refuse("manifest_already_exists", manifest_path=str(manifest_path))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(manifest, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, manifest_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        return _refuse("manifest_write_failed", manifest_path=str(manifest_path),
                       os_error=str(exc))
    return {**_base(), "written": str(manifest_path), "manifest": manifest}


def _load_json_arg(raw, name):
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, TypeError):
        return None, _refuse("invalid_json_argument", argument=name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Experiment manifests with multiple-testing accounting "
                    "(research lab only; never activates anything).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    apc = sub.add_parser("create", help="write <root>/<id>/manifest.json (no overwrite)")
    apc.add_argument("--id", required=True, help="experiment id ([A-Za-z0-9._-], <=64)")
    apc.add_argument("--root", default=str(DEFAULT_ROOT))
    apc.add_argument("--data-snapshot", action="append", required=True,
                     help="input data file to fingerprint (repeatable)")
    apc.add_argument("--param-grid", required=True, help="JSON object of the searched grid")
    apc.add_argument("--trials", required=True, type=int,
                     help="REQUIRED total number of variant evaluations (multiple-testing)")
    apc.add_argument("--selected", default="[]", help="JSON list of selected variants")
    apc.add_argument("--rejected", default="[]", help="JSON list of rejected variants")
    apc.add_argument("--note", default=None)

    apv = sub.add_parser("validate", help="validate an existing manifest.json")
    apv.add_argument("--path", default=None, help="path to manifest.json")
    apv.add_argument("--id", default=None, help="experiment id under --root")
    apv.add_argument("--root", default=str(DEFAULT_ROOT))

    args = ap.parse_args(argv)

    if args.cmd == "create":
        grid, err = _load_json_arg(args.param_grid, "param-grid")
        if err is None:
            selected, err = _load_json_arg(args.selected, "selected")
        if err is None:
            rejected, err = _load_json_arg(args.rejected, "rejected")
        if err is not None:
            print(json.dumps(err, indent=2))
            return 1
        result = create_manifest(args.id, args.root, args.data_snapshot, grid,
                                 args.trials, selected, rejected, note=args.note)
        print(json.dumps(result, indent=2))
        return 1 if "error" in result else 0

    # validate
    if args.path:
        path = Path(args.path)
    elif args.id:
        path = Path(args.root) / args.id / "manifest.json"
    else:
        print(json.dumps(_refuse("validate_requires_path_or_id"), indent=2))
        return 1
    if not path.is_file():
        print(json.dumps(_refuse("manifest_file_missing", manifest_path=str(path)),
                         indent=2))
        return 1
    try:
        manifest = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(json.dumps(_refuse("manifest_file_malformed", manifest_path=str(path)),
                         indent=2))
        return 1
    errors = validate_manifest(manifest)
    print(json.dumps({**_base(), "manifest_path": str(path), "valid": not errors,
                      "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
