#!/usr/bin/env python3
"""Roll immutable session feature packages into one validated research corpus.

The source packages remain immutable.  This module writes a replaceable derived
view with exact package/file/row lineage so unattended experiments can discover
the growing forward sample without parsing reports or accepting partial damage.
One malformed package blocks the whole corpus; it is never silently skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from quant import features as qf  # noqa: E402
from quant import session_capture as sc  # noqa: E402

TOOL = "quant.feature_corpus"
SCHEMA = "quant_feature_corpus/1.0"
MIN_REVIEWABLE = 30
STATUSES = {"empty", "accumulating", "reviewable", "blocked"}
EXPECTED_FILES = {"events.json", "bars.json", "features.json", "instruments.json"}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "network_invoked": False,
    "profitability_claim_allowed": False,
}


def _base() -> dict:
    return {**SAFETY, "tool": TOOL}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "corpus_sha256"}


def _read(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable_or_malformed:{type(exc).__name__}"


def validate_package(package_path: Path, package_root: Path) -> tuple[dict | None, list[str]]:
    package, problem = _read(package_path)
    if problem:
        return None, [f"package_{problem}"]
    errors = [f"package_safety_field_incorrect:{key}"
              for key, expected in SAFETY.items()
              if package.get(key) != expected]
    if package.get("schema") != sc.PACKAGE_SCHEMA:
        errors.append("package_schema_invalid")
    if package.get("activation_authority") is not False:
        errors.append("package_activation_authority_invalid")
    expected_hash = sc._sha_obj(sc._payload(package, "package_sha256"))
    if package.get("package_sha256") != expected_hash:
        errors.append("package_sha256_mismatch")
    files = package.get("files")
    if not isinstance(files, dict) or set(files) != EXPECTED_FILES:
        errors.append("package_files_invalid")
        files = {}
    try:
        resolved_package = package_path.resolve(strict=True)
        resolved_root = package_root.resolve(strict=True)
        resolved_package.relative_to(resolved_root)
    except (OSError, ValueError):
        errors.append("package_path_outside_root")
    documents: dict[str, dict] = {}
    for name in sorted(EXPECTED_FILES):
        path = package_path.parent / name
        doc, problem = _read(path)
        if problem:
            errors.append(f"{name}:{problem}")
            continue
        documents[name] = doc
        if not isinstance(files.get(name), str) or not _SHA_RE.fullmatch(files.get(name, "")):
            errors.append(f"{name}:recorded_hash_invalid")
        elif _sha(doc) != files[name]:
            errors.append(f"{name}:object_hash_mismatch")
    if errors:
        return None, sorted(set(errors))
    events = documents["events.json"]
    bars = documents["bars.json"]
    features = documents["features.json"]
    instruments = documents["instruments.json"]
    errors.extend(f"events:{value}" for value in qf.validate_event_snapshot(events))
    errors.extend(f"bars:{value}" for value in qf.validate_bars_archive(bars))
    errors.extend(f"features:{value}" for value in qf.validate_dataset(features))
    lineage = features.get("lineage") if isinstance(features.get("lineage"), dict) else {}
    if lineage.get("bars_payload_sha256") != qf._sha256_obj(bars):
        errors.append("feature_bars_payload_binding_mismatch")
    if lineage.get("events_payload_sha256") != qf._sha256_obj(events):
        errors.append("feature_events_payload_binding_mismatch")
    if any(instruments.get(key) != expected for key, expected in qf.SAFETY_FIELDS.items()):
        errors.append("instruments_safety_invalid")
    mapping = instruments.get("instrument_types")
    if not isinstance(mapping, dict):
        errors.append("instrument_types_invalid")
        mapping = {}
    event_by_id = {event.get("event_id"): event for event in events.get("events", [])}
    feature_ids = {row.get("event_id") for row in features.get("rows", [])}
    if feature_ids != set(event_by_id):
        errors.append("event_feature_id_set_mismatch")
    if package.get("target_session_date") != instruments.get("target_session_date"):
        errors.append("package_instrument_target_mismatch")
    for event_id, event in event_by_id.items():
        symbol = event.get("symbol")
        if event.get("instrument_type") != mapping.get(symbol):
            errors.append(f"instrument_type_binding_mismatch:{event_id}")
    if package.get("event_count") != len(event_by_id):
        errors.append("package_event_count_mismatch")
    eligible = sum(row.get("eligible_for_modeling") is True
                   for row in features.get("rows", []))
    if package.get("eligible_feature_row_count") != eligible:
        errors.append("package_eligible_count_mismatch")
    if errors:
        return None, sorted(set(errors))
    return {"package": package, "documents": documents,
            "event_by_id": event_by_id}, []


def build_corpus(package_root: Path) -> dict:
    package_paths = sorted(package_root.glob("*/package.json")) if package_root.is_dir() else []
    source_packages = []
    rows = []
    row_sources = {}
    instrument_types = {}
    blockers = []
    seen_events: set[str] = set()
    for package_path in package_paths:
        validated, errors = validate_package(package_path, package_root)
        relative = str(package_path.relative_to(package_root))
        if errors:
            blockers.extend(f"{relative}:{value}" for value in errors)
            continue
        package = validated["package"]
        documents = validated["documents"]
        features = documents["features.json"]
        events = validated["event_by_id"]
        mapping = documents["instruments.json"]["instrument_types"]
        source_packages.append({
            "target_session_date": package["target_session_date"],
            "path": relative,
            "package_sha256": package["package_sha256"],
            "feature_dataset_sha256": features["dataset_sha256"],
            "row_count": len(features["rows"]),
        })
        for row in features["rows"]:
            event_id = row["event_id"]
            if event_id in seen_events:
                blockers.append(f"duplicate_event_id_across_packages:{event_id}")
                continue
            seen_events.add(event_id)
            rows.append(row)
            row_sources[event_id] = {
                "package_sha256": package["package_sha256"],
                "feature_dataset_sha256": features["dataset_sha256"],
                "row_sha256": row["row_sha256"],
            }
            instrument_types[event_id] = mapping[events[event_id]["symbol"]]
    if blockers:
        source_packages = []
        rows = []
        row_sources = {}
        instrument_types = {}
        status = "blocked"
    else:
        rows.sort(key=lambda value: (value["event_date"], value["event_id"]))
        eligible = sum(row["eligible_for_modeling"] is True for row in rows)
        status = "empty" if not rows else (
            "reviewable" if eligible >= MIN_REVIEWABLE else "accumulating")
    corpus = {
        **_base(),
        "ok": True,
        "schema": SCHEMA,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "minimum_reviewable": MIN_REVIEWABLE,
        "package_count": len(source_packages),
        "row_count": len(rows),
        "eligible_row_count": sum(row.get("eligible_for_modeling") is True for row in rows),
        "source_packages": source_packages,
        "rows": rows,
        "rows_sha256": _sha(rows),
        "row_sources": row_sources,
        "instrument_types": instrument_types,
        "blockers": sorted(set(blockers)),
        "activation_authority": False,
    }
    corpus["corpus_sha256"] = _sha(_payload(corpus))
    errors = validate_corpus(corpus)
    if errors:
        return {**_base(), "ok": False, "error": "corpus_self_validation_failed",
                "errors": errors}
    return corpus


def validate_corpus(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["corpus_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in SAFETY.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True or doc.get("schema") != SCHEMA:
        errors.append("corpus_schema_or_ok_invalid")
    if doc.get("status") not in STATUSES:
        errors.append("status_invalid")
    if not isinstance(doc.get("minimum_reviewable"), int) or doc.get("minimum_reviewable") < 1:
        errors.append("minimum_reviewable_invalid")
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return errors + ["rows_invalid"]
    if doc.get("row_count") != len(rows):
        errors.append("row_count_mismatch")
    eligible = sum(isinstance(row, dict) and row.get("eligible_for_modeling") is True
                   for row in rows)
    if doc.get("eligible_row_count") != eligible:
        errors.append("eligible_row_count_mismatch")
    if doc.get("rows_sha256") != _sha(rows):
        errors.append("rows_sha256_mismatch")
    ids = [row.get("event_id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or len(set(ids)) != len(ids):
        errors.append("event_ids_invalid_or_duplicate")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if any(row.get(key) != expected for key, expected in qf.SAFETY_FIELDS.items()):
            errors.append(f"row[{index}].safety_invalid")
        if row.get("row_sha256") != qf._sha256_obj(qf._row_payload(row)):
            errors.append(f"row[{index}].row_sha256_mismatch")
    sources = doc.get("row_sources")
    instruments = doc.get("instrument_types")
    row_hashes = {row.get("event_id"): row.get("row_sha256") for row in rows
                  if isinstance(row, dict)}
    if not isinstance(sources, dict) or set(sources) != set(ids):
        errors.append("row_sources_invalid")
    elif any(not isinstance(value, dict)
             or value.get("row_sha256") != row_hashes.get(event_id)
             or any(not isinstance(value.get(field), str)
                    or not _SHA_RE.fullmatch(value.get(field, ""))
                    for field in ("package_sha256", "feature_dataset_sha256", "row_sha256"))
             for event_id, value in sources.items()):
        errors.append("row_source_hash_binding_invalid")
    if not isinstance(instruments, dict) or set(instruments) != set(ids):
        errors.append("instrument_types_invalid")
    elif any(value not in sc.qb_instruments() for value in instruments.values()):
        errors.append("instrument_type_prohibited_or_unknown")
    packages = doc.get("source_packages")
    if not isinstance(packages, list) or doc.get("package_count") != len(packages):
        errors.append("source_packages_invalid")
    blockers = doc.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(value, str) for value in blockers):
        errors.append("blockers_invalid")
    if doc.get("status") == "blocked" and not blockers:
        errors.append("blocked_without_reason")
    if doc.get("status") != "blocked" and blockers:
        errors.append("nonblocked_with_blockers")
    if doc.get("status") != "blocked":
        expected_status = ("empty" if not rows else
                           "reviewable" if eligible >= doc.get("minimum_reviewable", math.inf)
                           else "accumulating")
        if doc.get("status") != expected_status:
            errors.append("sample_status_inconsistent")
    if doc.get("activation_authority") is not False:
        errors.append("activation_authority_invalid")
    if doc.get("corpus_sha256") != _sha(_payload(doc)):
        errors.append("corpus_sha256_mismatch")
    return sorted(set(errors))


def _atomic_write(path: Path, doc: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_feature_corpus_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Roll immutable session features into a corpus.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--package-root", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        doc, problem = _read(Path(args.path))
        if problem:
            print(json.dumps({**_base(), "ok": False, "error": problem}, indent=2))
            return 1
        errors = validate_corpus(doc)
        print(json.dumps({**_base(), "ok": not errors, "errors": errors},
                         indent=2, sort_keys=True))
        return 0 if not errors else 1
    corpus = build_corpus(Path(args.package_root))
    if corpus.get("ok") is not True:
        print(json.dumps(corpus, indent=2, sort_keys=True))
        return 1
    write_error = _atomic_write(Path(args.out), corpus, args.replace)
    if write_error:
        print(json.dumps({**_base(), "ok": False, "error": write_error}, indent=2))
        return 1
    print(json.dumps({**_base(), "ok": corpus["status"] != "blocked",
                      "status": corpus["status"], "row_count": corpus["row_count"],
                      "written": args.out, "corpus_sha256": corpus["corpus_sha256"]},
                     indent=2, sort_keys=True))
    return 1 if corpus["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
