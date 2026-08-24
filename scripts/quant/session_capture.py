#!/usr/bin/env python3
"""Capture one immutable pre-event feature package from the morning research artifacts.

This post-close-safe collector joins only already-frozen local artifacts:

* the target session's self-hashed data-lake readiness receipt and snapshot;
* its validated catalyst graph;
* point-in-time security-master intervals; and
* adjusted daily bars observed by the snapshot cutoff.

It emits no forecast and performs no network, model, or broker call.  Confirmed,
non-binary, non-dilutive events are transformed into a ``quant_event_snapshot``
and then into the existing prior-close feature schema.  The immutable package is
the forward-testing input that can later be settled against appended outcome bars.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import catalyst_graph  # noqa: E402
import data_lake_ingest  # noqa: E402
import data_lake_intake  # noqa: E402
import data_lake_snapshot  # noqa: E402
import universe_master  # noqa: E402
from provider_adapters import base  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.session_capture"
SCHEMA = "quant_session_capture/1.0"
PACKAGE_SCHEMA = "quant_session_feature_package/1.0"
STATUSES = {"captured", "data_not_ready", "graph_unavailable", "graph_not_ready",
            "no_eligible_events", "blocked"}
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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _payload(doc: dict, field: str) -> dict:
    return {key: value for key, value in doc.items() if key != field}


def _health(target: str, status: str) -> dict:
    return {
        **_base(),
        "ok": True,
        "schema": SCHEMA,
        "target_session_date": target,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "source_readiness_sha256": None,
        "source_graph_sha256": None,
        "source_snapshot_sha256": None,
        "package_path": None,
        "package_sha256": None,
        "event_count": 0,
        "eligible_feature_row_count": 0,
        "blockers": [],
        "exclusions": [],
        "activation_authority": False,
    }


def _finish(doc: dict) -> dict:
    doc["capture_sha256"] = _sha_obj(_payload(doc, "capture_sha256"))
    return doc


def validate_health(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["capture_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in SAFETY.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True or doc.get("schema") != SCHEMA:
        errors.append("capture_schema_or_ok_invalid")
    try:
        target = dt.date.fromisoformat(doc.get("target_session_date", ""))
        if target.isoformat() != doc.get("target_session_date"):
            errors.append("target_invalid")
    except (TypeError, ValueError):
        errors.append("target_invalid")
    if base.parse_utc(doc.get("generated_utc")) is None:
        errors.append("generated_utc_invalid")
    if doc.get("status") not in STATUSES:
        errors.append("status_invalid")
    for key in ("event_count", "eligible_feature_row_count"):
        if not isinstance(doc.get(key), int) or isinstance(doc.get(key), bool) or doc[key] < 0:
            errors.append(f"{key}_invalid")
    for key in ("blockers", "exclusions"):
        if not isinstance(doc.get(key), list) or any(not isinstance(value, str)
                                                    for value in doc.get(key, [])):
            errors.append(f"{key}_invalid")
    if doc.get("activation_authority") is not False:
        errors.append("activation_authority_must_be_false")
    if doc.get("capture_sha256") != _sha_obj(_payload(doc, "capture_sha256")):
        errors.append("capture_sha256_mismatch")
    return sorted(set(errors))


def _instrument_map(records: list[dict], target: str) -> tuple[dict[str, str], list[str]]:
    queried = universe_master.query_as_of(records, target)
    grouped: dict[str, list[dict]] = {}
    for record in queried["records"]:
        grouped.setdefault(record["symbol"], []).append(record)
    mapping: dict[str, str] = {}
    exclusions: list[str] = []
    for symbol, values in grouped.items():
        eligible = {(value.get("security_type"), value.get("eligible_instrument_type"))
                    for value in values}
        if len(eligible) != 1:
            exclusions.append(f"instrument_identity_ambiguous:{symbol}")
            continue
        security_type, allowed = next(iter(eligible))
        if allowed is True and security_type in qb_instruments():
            mapping[symbol] = security_type
        else:
            exclusions.append(f"instrument_ineligible:{symbol}")
    return mapping, exclusions


def qb_instruments() -> set[str]:
    return {"common_stock", "unleveraged_etf"}


def events_from_graph(graph: dict, target: str,
                      instrument_types: dict[str, str]) -> tuple[dict | None, list[str]]:
    cutoff = graph.get("source_evidence_generated_utc")
    if base.parse_utc(cutoff) is None:
        return None, ["graph_evidence_cutoff_invalid"]
    nodes = {node.get("id"): node for node in graph.get("nodes", [])
             if isinstance(node, dict) and isinstance(node.get("id"), str)}
    sources_by_event: dict[str, list[dict]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") not in {"confirms", "supports"}:
            continue
        source, event = nodes.get(edge.get("from")), nodes.get(edge.get("to"))
        if source and event and source.get("type") == "source_document" \
                and event.get("type") == "event":
            sources_by_event.setdefault(event["id"], []).append(source)
    events = []
    exclusions = []
    for verdict in graph.get("event_verdicts", []):
        if not isinstance(verdict, dict):
            continue
        event_id = verdict.get("event_id")
        symbol = verdict.get("symbol")
        flags = verdict.get("flags") if isinstance(verdict.get("flags"), dict) else {}
        reasons = []
        if verdict.get("verdict") != "confirmed":
            reasons.append("not_confirmed")
        for key in ("binary_event", "dilution_financing", "stale_or_expired", "rumored",
                    "unresolved_conflict"):
            if flags.get(key) is True:
                reasons.append(key)
        if symbol not in instrument_types:
            reasons.append("instrument_not_point_in_time_eligible")
        sources = sources_by_event.get(event_id, [])
        observed = [base.parse_utc(source.get("observed_at_utc")) for source in sources]
        observed = [value for value in observed if value is not None]
        if not observed:
            reasons.append("source_observation_time_missing")
        if reasons:
            exclusions.append(f"{event_id}:" + ",".join(sorted(set(reasons))))
            continue
        latest = max(observed).isoformat()
        events.append({
            "event_id": event_id,
            "symbol": symbol,
            "event_date": target,
            "observed_at_utc": latest,
            "structured_features": {
                "catalyst_type": verdict.get("event_category") or "other",
                "source_quality": verdict.get("evidence_strength") or "primary_confirmed",
                "dilution_risk": False,
            },
            "instrument_type": instrument_types[symbol],
            "source_graph_sha256": graph.get("graph_sha256"),
        })
    snapshot = {"schema": qf.EVENT_SCHEMA, "knowledge_cutoff_utc": cutoff,
                "events": sorted(events, key=lambda value: value["event_id"])}
    errors = qf.validate_event_snapshot(snapshot)
    if errors:
        return None, exclusions + [f"event_snapshot:{error}" for error in errors]
    return snapshot, exclusions


def _snapshot_and_instruments(receipt: dict, config: dict, target: str
                              ) -> tuple[dict | None, Path | None, dict[str, str], list[str]]:
    summary = receipt.get("snapshot") if isinstance(receipt.get("snapshot"), dict) else {}
    raw_path = summary.get("snapshot_path")
    if not isinstance(raw_path, str):
        return None, None, {}, ["snapshot_path_missing"]
    snapshot_root = data_lake_ingest.configured_path(config, "snapshot_root").resolve()
    snapshot_path = Path(raw_path)
    if not snapshot_path.is_absolute():
        snapshot_path = ROOT / snapshot_path
    try:
        resolved = snapshot_path.resolve(strict=True)
        resolved.relative_to(snapshot_root)
    except (OSError, ValueError):
        return None, None, {}, ["snapshot_path_outside_configured_root_or_missing"]
    try:
        snapshot = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, {}, ["snapshot_unreadable_or_malformed"]
    lake_root = data_lake_ingest.configured_path(config, "lake_root")
    validation = data_lake_snapshot.validate_snapshot(snapshot, lake_root)
    if not validation.get("ok"):
        return None, None, {}, [f"snapshot:{value}" for value in validation.get("errors", [])]
    if snapshot.get("snapshot_id") != summary.get("snapshot_id"):
        return None, None, {}, ["readiness_snapshot_id_mismatch"]
    records = []
    for entry in snapshot["manifests"]:
        if entry.get("dataset") == "security_master":
            records.extend(data_lake_snapshot._manifest_records(lake_root, entry))
    instruments, exclusions = _instrument_map(records, target)
    if not instruments:
        exclusions.append("no_point_in_time_eligible_instruments")
    return snapshot, lake_root, instruments, exclusions


def _write_package(output_root: Path, target: str, documents: dict[str, dict],
                   package: dict) -> tuple[str | None, str | None]:
    destination = output_root / target
    if destination.exists():
        try:
            existing = json.loads((destination / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "existing_package_unreadable"
        if existing.get("package_sha256") == package.get("package_sha256"):
            return str(destination), None
        return None, "immutable_package_conflict"
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target}.tmp-", dir=str(output_root)))
    try:
        for name, document in documents.items():
            (temporary / name).write_text(
                json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8")
        (temporary / "package.json").write_text(
            json.dumps(package, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8")
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return str(destination), None


def run_capture(*, target: str, readiness_path: Path, graph_path: Path,
                config_path: Path, output_root: Path) -> tuple[dict, int]:
    result = _health(target, "blocked")
    try:
        target_date = dt.date.fromisoformat(target)
        if target_date.isoformat() != target:
            raise ValueError
    except (TypeError, ValueError):
        result["blockers"] = ["target_invalid"]
        return _finish(result), 1
    try:
        receipt = json.loads(readiness_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["blockers"] = ["readiness_missing"]
        return _finish(result), 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        result["blockers"] = ["readiness_unreadable_or_malformed"]
        return _finish(result), 1
    receipt_errors = data_lake_intake.validate_receipt(receipt)
    if receipt_errors or receipt.get("target_session_date") != target:
        result["blockers"] = sorted(set(
            [f"readiness:{value}" for value in receipt_errors]
            + (["readiness_target_mismatch"] if receipt.get("target_session_date") != target else [])))
        return _finish(result), 1
    result["source_readiness_sha256"] = receipt["receipt_sha256"]
    if receipt.get("research_ready") is not True:
        quality = receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {}
        result["status"] = "data_not_ready"
        result["blockers"] = sorted(set((quality.get("readiness_blockers") or [])
                                        + (quality.get("warnings") or [])))
        return _finish(result), 0

    config, config_errors = data_lake_ingest.load_config(config_path)
    if config is None or config_errors:
        result["blockers"] = [f"data_config:{value}" for value in config_errors]
        return _finish(result), 1
    if not graph_path.is_file():
        result["status"] = "graph_unavailable"
        result["blockers"] = ["catalyst_graph_missing"]
        return _finish(result), 0
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        result["blockers"] = ["catalyst_graph_unreadable_or_malformed"]
        return _finish(result), 1
    graph_errors = catalyst_graph.validate_graph(graph, root=ROOT)
    if graph_errors:
        result["blockers"] = [f"catalyst_graph:{value}" for value in graph_errors]
        return _finish(result), 1
    result["source_graph_sha256"] = graph["graph_sha256"]
    if graph.get("build_status") != "ready":
        result["status"] = "graph_not_ready"
        result["blockers"] = list(graph.get("blockers") or ["graph_build_not_ready"])
        return _finish(result), 0

    snapshot, lake_root, instruments, exclusions = _snapshot_and_instruments(
        receipt, config, target)
    result["exclusions"] = sorted(set(exclusions))
    if snapshot is None or lake_root is None:
        result["blockers"] = sorted(set(exclusions))
        return _finish(result), 1
    result["source_snapshot_sha256"] = snapshot["snapshot_sha256"]
    events, event_exclusions = events_from_graph(graph, target, instruments)
    result["exclusions"] = sorted(set(result["exclusions"] + event_exclusions))
    if events is None:
        result["blockers"] = [value for value in event_exclusions
                              if value.startswith("event_snapshot:")]
        return _finish(result), 1
    if not events["events"]:
        result["status"] = "no_eligible_events"
        return _finish(result), 0

    bars_result = data_lake_snapshot.materialize_bars(
        snapshot, lake_root, adjustment="adjusted")
    if not bars_result.get("ok"):
        result["blockers"] = [f"bar_materialization:{bars_result.get('error', 'unknown')}"]
        result["exclusions"].extend(bars_result.get("conflicts") or [])
        return _finish(result), 1
    bars = bars_result["bar_snapshot"]
    features = qf.build_dataset(
        bars, events, bars_file_sha256=_sha_obj(bars),
        events_file_sha256=_sha_obj(events))
    if features.get("ok") is not True:
        result["blockers"] = [f"feature_build:{features.get('error', 'unknown')}"]
        result["exclusions"].extend(features.get("errors") or [])
        return _finish(result), 1
    documents = {
        "events.json": events,
        "bars.json": bars,
        "features.json": features,
        "instruments.json": {"target_session_date": target,
                             "instrument_types": instruments,
                             **qf.SAFETY_FIELDS},
    }
    file_hashes = {name: _sha_obj(document) for name, document in documents.items()}
    package = {
        **_base(),
        "schema": PACKAGE_SCHEMA,
        "target_session_date": target,
        "source_readiness_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": graph["graph_sha256"],
        "source_snapshot_sha256": snapshot["snapshot_sha256"],
        "event_count": len(events["events"]),
        "eligible_feature_row_count": sum(
            row["eligible_for_modeling"] is True for row in features["rows"]),
        "files": file_hashes,
        "activation_authority": False,
    }
    package["package_sha256"] = _sha_obj(package)
    package_path, package_error = _write_package(output_root, target, documents, package)
    if package_error:
        result["blockers"] = [package_error]
        return _finish(result), 1
    result.update({
        "status": "captured",
        "package_path": package_path,
        "package_sha256": package["package_sha256"],
        "event_count": package["event_count"],
        "eligible_feature_row_count": package["eligible_feature_row_count"],
    })
    return _finish(result), 0


def _atomic_write(path: Path, doc: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_session_capture_",
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
    parser = argparse.ArgumentParser(description="Immutable daily quant feature capture.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--target", required=True)
    run.add_argument("--readiness", required=True)
    run.add_argument("--graph", required=True)
    run.add_argument("--config", default=str(data_lake_ingest.CONFIG_DEFAULT))
    run.add_argument("--package-root", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            doc = json.loads(Path(args.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(json.dumps({**_base(), "ok": False,
                              "error": f"unreadable_or_malformed:{type(exc).__name__}"},
                             indent=2, sort_keys=True))
            return 1
        errors = validate_health(doc)
        print(json.dumps({**_base(), "ok": not errors, "errors": errors},
                         indent=2, sort_keys=True))
        return 0 if not errors else 1
    doc, rc = run_capture(
        target=args.target, readiness_path=Path(args.readiness), graph_path=Path(args.graph),
        config_path=Path(args.config), output_root=Path(args.package_root))
    write_error = _atomic_write(Path(args.out), doc, args.replace)
    if write_error:
        print(json.dumps({**_base(), "ok": False, "error": write_error},
                         indent=2, sort_keys=True))
        return 1
    print(json.dumps({**_base(), "ok": rc == 0, "status": doc["status"],
                      "written": args.out, "capture_sha256": doc["capture_sha256"]},
                     indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
