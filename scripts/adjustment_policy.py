#!/usr/bin/env python3
"""Cutoff-aware corporate-action adjustment views for the point-in-time lake.

Raw provider bars remain immutable. This module derives reproducible raw, split-only, and
research total-return views from one validated snapshot and one explicitly selected provider.
It grants no production, strategy, Buy, risk, broker, or execution authority.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from provider_adapters import base

ROOT = Path(__file__).resolve().parent.parent
POLICY_DEFAULT = ROOT / "config" / "adjustment_policy.json"
POLICY_SCHEMA = "adjustment_policy_config/1.0"
VIEW_SCHEMA = "adjusted_bar_view/1.0"

SUPPORTED_VIEWS = ("raw", "split_only", "total_return")
SUPPORTED_ACTIONS = ("split", "cash_dividend")
ACTION_STATUSES = {"active", "corrected", "rescinded"}
POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production_scoring": False,
    "strategy_change_allowed": False,
    "raw_bars_immutable": True,
    "provider_adjusted_values_are_not_relabelled_as_policy_views": True,
    "source_provider_required": True,
    "broker_access": "none",
}
VIEW_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production_scoring": False,
    "strategy_change_allowed": False,
    "broker_access": "none",
}
_POLICY_TOP = {
    "schema", "version", "supported_views", "supported_action_types",
    "split_ratio_convention", "split_price_rule", "split_volume_rule",
    "cash_dividend_price_rule", "cash_dividend_reference_rule",
    "cash_dividend_currency", "effective_date_rule", "future_observation_rule",
    "future_effective_action_rule", "revision_rule", "unsupported_action_rule",
    "output_decimal_places", "minimum_cash_factor", "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "supported_views": list(SUPPORTED_VIEWS),
    "supported_action_types": list(SUPPORTED_ACTIONS),
    "split_ratio_convention": "new_shares_per_old_share",
    "split_price_rule": "pre_effective_prices_divided_by_ratio",
    "split_volume_rule": "pre_effective_volume_multiplied_by_ratio",
    "cash_dividend_price_rule": (
        "pre_ex_date_prices_times_previous_close_minus_cash_over_previous_close"),
    "cash_dividend_reference_rule": "latest_unadjusted_close_before_effective_date",
    "cash_dividend_currency": "USD",
    "effective_date_rule": "apply_only_when_effective_date_not_after_snapshot_cutoff_date",
    "future_observation_rule": "reject_record_observed_after_snapshot_cutoff",
    "future_effective_action_rule": "exclude_without_adjusting_history",
    "revision_rule": "linear_contiguous_hash_chain_latest_revision_wins_rescind_removes",
    "unsupported_action_rule": "fail_closed",
}
_ACTION_KEYS = {
    "action_id", "action_type", "symbol", "security_id", "effective_date", "status",
    "revision", "previous_record_sha256", "split_ratio_numerator",
    "split_ratio_denominator", "cash_amount", "currency", "original_provider",
}
_VIEW_TOP = {
    "schema", "policy_version", "policy_sha256", "snapshot_id", "snapshot_sha256",
    "knowledge_cutoff_utc", "as_of_date", "view", "source_provider", "bars",
    "bar_lineage", "action_lineage", "counts", "roundtrip", "quant_compatible",
    "quant_validation_errors", "bars_payload_sha256", "lineage_sha256", "dataset_id",
    *VIEW_SAFETY.keys(), "view_sha256",
}
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_INPUT_BYTES = 64 * 1024 * 1024


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_once(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("output_already_exists")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _decimal(value: Any, *, positive: bool = False,
             nonnegative: bool = False) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    if not result.is_finite():
        return None
    if positive and result <= 0:
        return None
    if nonnegative and result < 0:
        return None
    return result


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read_json(path)
    except Exception:
        return None, ["adjustment_policy_unreadable"]
    return document, validate_policy(document)


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["adjustment_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_TOP:
        errors.append("adjustment_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"adjustment_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            str(document.get("version") or "")):
        errors.append("adjustment_policy_version_invalid")
    places = document.get("output_decimal_places")
    if not isinstance(places, int) or not 4 <= places <= 12:
        errors.append("adjustment_policy_decimal_places_invalid")
    minimum = _decimal(document.get("minimum_cash_factor"), positive=True)
    if minimum is None or minimum >= 1:
        errors.append("adjustment_policy_minimum_cash_factor_invalid")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("adjustment_policy_safety_invalid")
    return sorted(set(errors))


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    valid = policy is not None and not errors
    return {
        "ok": valid,
        "valid": valid,
        "errors": errors,
        "version": policy.get("version") if isinstance(policy, dict) else None,
        "policy_sha256": base.sha256_obj(policy) if isinstance(policy, dict) else None,
        "supported_views": policy.get("supported_views") if isinstance(policy, dict) else [],
        **VIEW_SAFETY,
    }


def validate_action_record(record: Any, policy: dict,
                           source_provider: str | None = None) -> list[str]:
    errors = [f"normalized_record:{error}" for error in base.validate_record(record)]
    if not isinstance(record, dict):
        return errors or ["action_record_not_object"]
    if record.get("dataset") != "corporate_actions":
        errors.append("action_record_dataset_invalid")
    payload = record.get("payload")
    if not isinstance(payload, dict) or set(payload) != _ACTION_KEYS:
        errors.append("action_payload_shape_invalid")
        return sorted(set(errors))
    for key in ("action_id", "original_provider"):
        if not isinstance(payload.get(key), str) or not _TOKEN_RE.fullmatch(
                str(payload.get(key) or "")):
            errors.append(f"action_payload_token_invalid:{key}")
    if source_provider is not None and payload.get("original_provider") != source_provider:
        errors.append("action_source_provider_mismatch")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        errors.append("action_symbol_invalid")
    if record.get("symbol") != symbol:
        errors.append("action_record_symbol_mismatch")
    security_id = payload.get("security_id")
    if security_id is not None and (
            not isinstance(security_id, str) or not _TOKEN_RE.fullmatch(security_id)):
        errors.append("action_security_id_invalid")
    if payload.get("action_type") not in policy.get("supported_action_types", []):
        errors.append("action_type_unsupported")
    if _date(payload.get("effective_date")) is None:
        errors.append("action_effective_date_invalid")
    if payload.get("status") not in ACTION_STATUSES:
        errors.append("action_status_invalid")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append("action_revision_invalid")
    previous = payload.get("previous_record_sha256")
    if previous is not None and (
            not isinstance(previous, str) or not _SHA_RE.fullmatch(previous)):
        errors.append("action_previous_record_sha256_invalid")
    action_type = payload.get("action_type")
    if action_type == "split":
        if _decimal(payload.get("split_ratio_numerator"), positive=True) is None:
            errors.append("split_ratio_numerator_invalid")
        if _decimal(payload.get("split_ratio_denominator"), positive=True) is None:
            errors.append("split_ratio_denominator_invalid")
        if payload.get("cash_amount") is not None or payload.get("currency") is not None:
            errors.append("split_payload_has_dividend_fields")
    elif action_type == "cash_dividend":
        if payload.get("split_ratio_numerator") is not None or payload.get(
                "split_ratio_denominator") is not None:
            errors.append("dividend_payload_has_split_fields")
        if _decimal(payload.get("cash_amount"), positive=True) is None:
            errors.append("cash_amount_invalid")
        if payload.get("currency") != policy.get("cash_dividend_currency"):
            errors.append("cash_dividend_currency_invalid")
    return sorted(set(errors))


def _collect_action_chains(snapshot: dict, lake_root: Path, policy: dict,
                           source_provider: str) -> tuple[list[dict], list[dict], list[str]]:
    import data_lake_snapshot as lake_snapshot

    cutoff = base.parse_utc(snapshot.get("knowledge_cutoff_utc"))
    as_of = cutoff.date() if cutoff is not None else None
    grouped: dict[str, list[dict]] = {}
    errors: list[str] = []
    matching_record_count = 0
    for entry in snapshot.get("manifests") or []:
        if entry.get("dataset") != "corporate_actions":
            continue
        for record in lake_snapshot._manifest_records(lake_root, entry):
            payload = record.get("payload") or {}
            provider = payload.get("original_provider") or record.get("provider")
            if provider != source_provider:
                continue
            matching_record_count += 1
            record_errors = validate_action_record(record, policy, source_provider)
            if record_errors:
                errors.extend(f"{record.get('record_id')}:{error}" for error in record_errors)
                continue
            observed = base.parse_utc(record.get("observed_at_utc"))
            if cutoff is None or observed is None or observed > cutoff:
                errors.append(f"action_observed_after_snapshot_cutoff:{record.get('record_id')}")
                continue
            grouped.setdefault(payload["action_id"], []).append(record)
    if matching_record_count == 0:
        errors.append("corporate_action_dataset_missing_or_empty")
    selected: list[dict] = []
    lineage: list[dict] = []
    immutable_keys = ("action_id", "action_type", "symbol", "security_id", "original_provider")
    for action_id, records in sorted(grouped.items()):
        by_revision: dict[int, list[dict]] = {}
        for record in records:
            by_revision.setdefault(record["payload"]["revision"], []).append(record)
        duplicate_revisions = [revision for revision, rows in by_revision.items() if len(rows) != 1]
        if duplicate_revisions:
            errors.append(f"action_revision_duplicate:{action_id}")
            continue
        revisions = [by_revision[number][0] for number in sorted(by_revision)]
        if [row["payload"]["revision"] for row in revisions] != list(
                range(1, len(revisions) + 1)):
            errors.append(f"action_revision_gap:{action_id}")
            continue
        chain_error = False
        first_payload = revisions[0]["payload"]
        if first_payload["status"] != "active" or first_payload["previous_record_sha256"] is not None:
            errors.append(f"action_revision_one_invalid:{action_id}")
            chain_error = True
        previous_record = None
        previous_observed = None
        rescinded_seen = False
        for index, record in enumerate(revisions):
            payload = record["payload"]
            observed = base.parse_utc(record["observed_at_utc"])
            if any(payload[key] != first_payload[key] for key in immutable_keys):
                errors.append(f"action_revision_identity_changed:{action_id}:{index + 1}")
                chain_error = True
            if index > 0:
                if payload["status"] not in {"corrected", "rescinded"}:
                    errors.append(f"action_revision_status_invalid:{action_id}:{index + 1}")
                    chain_error = True
                if payload["previous_record_sha256"] != previous_record["record_sha256"]:
                    errors.append(f"action_revision_parent_mismatch:{action_id}:{index + 1}")
                    chain_error = True
                if previous_observed is None or observed is None or observed <= previous_observed:
                    errors.append(f"action_revision_time_not_increasing:{action_id}:{index + 1}")
                    chain_error = True
            if rescinded_seen:
                errors.append(f"action_revision_after_rescind:{action_id}:{index + 1}")
                chain_error = True
            rescinded_seen = payload["status"] == "rescinded"
            previous_record, previous_observed = record, observed
        if chain_error:
            continue
        latest = revisions[-1]
        latest_payload = latest["payload"]
        effective = _date(latest_payload["effective_date"])
        if latest_payload["status"] == "rescinded":
            state = "rescinded"
        elif as_of is not None and effective is not None and effective > as_of:
            state = "known_future_not_effective"
        else:
            state = "active_effective"
            selected.append(latest)
        lineage.append({
            "action_id": action_id,
            "action_type": latest_payload["action_type"],
            "symbol": latest_payload["symbol"],
            "effective_date": latest_payload["effective_date"],
            "revision_record_sha256s": [row["record_sha256"] for row in revisions],
            "selected_record_sha256": latest["record_sha256"],
            "state": state,
        })
    return selected, lineage, sorted(set(errors))


def _round_decimal(value: Decimal, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    output = float(rounded)
    return 0.0 if output == 0 else output


def _view_identity(document: dict) -> dict:
    return {
        "schema": document.get("schema"),
        "policy_version": document.get("policy_version"),
        "policy_sha256": document.get("policy_sha256"),
        "snapshot_id": document.get("snapshot_id"),
        "view": document.get("view"),
        "source_provider": document.get("source_provider"),
        "bars_payload_sha256": document.get("bars_payload_sha256"),
        "lineage_sha256": document.get("lineage_sha256"),
    }


def _view_payload(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "view_sha256"}


def _prepare_actions(actions: list[dict], raw_bars: dict[str, list[dict]], policy: dict,
                     view: str) -> tuple[list[dict], list[str]]:
    prepared: list[dict] = []
    errors: list[str] = []
    minimum_factor = _decimal(policy["minimum_cash_factor"], positive=True) or Decimal("0")
    for record in actions:
        payload = record["payload"]
        item = {
            "record_sha256": record["record_sha256"],
            "action_id": payload["action_id"],
            "action_type": payload["action_type"],
            "symbol": payload["symbol"],
            "effective_date": payload["effective_date"],
            "price_factor": Decimal("1"),
            "volume_factor": Decimal("1"),
            "applies_to_view": payload["action_type"] == "split" or view == "total_return",
        }
        if payload["action_type"] == "split":
            numerator = _decimal(payload["split_ratio_numerator"], positive=True)
            denominator = _decimal(payload["split_ratio_denominator"], positive=True)
            if numerator is None or denominator is None:
                errors.append(f"split_ratio_invalid:{payload['action_id']}")
                continue
            item["price_factor"] = denominator / numerator
            item["volume_factor"] = numerator / denominator
        elif view == "total_return":
            prior = [row for row in raw_bars.get(payload["symbol"], [])
                     if row["date"] < payload["effective_date"]]
            if not prior:
                errors.append(f"dividend_reference_close_missing:{payload['action_id']}")
                continue
            previous_close = _decimal(prior[-1].get("close"), positive=True)
            cash = _decimal(payload.get("cash_amount"), positive=True)
            if previous_close is None or cash is None or cash >= previous_close:
                errors.append(f"dividend_factor_invalid:{payload['action_id']}")
                continue
            factor = (previous_close - cash) / previous_close
            if factor < minimum_factor:
                errors.append(f"dividend_factor_below_minimum:{payload['action_id']}")
                continue
            item["price_factor"] = factor
            item["dividend_reference_bar_date"] = prior[-1]["date"]
            item["dividend_reference_close"] = str(previous_close)
        prepared.append(item)
    prepared.sort(key=lambda row: (row["effective_date"], row["action_id"], row["record_sha256"]))
    return prepared, sorted(set(errors))


def materialize_view(snapshot: dict, lake_root: Path, *, policy: dict, view: str,
                     source_provider: str) -> dict:
    import data_lake_snapshot as lake_snapshot
    from quant import features

    policy_errors = validate_policy(policy)
    if policy_errors:
        return {"ok": False, "error": "adjustment_policy_invalid",
                "errors": policy_errors, **VIEW_SAFETY}
    if view not in policy["supported_views"]:
        return {"ok": False, "error": "adjustment_view_invalid", **VIEW_SAFETY}
    if not isinstance(source_provider, str) or not _TOKEN_RE.fullmatch(source_provider):
        return {"ok": False, "error": "source_provider_invalid", **VIEW_SAFETY}
    snapshot_check = lake_snapshot.validate_snapshot(snapshot, lake_root)
    if not snapshot_check["ok"]:
        return {"ok": False, "error": "snapshot_invalid",
                "errors": snapshot_check["errors"], **VIEW_SAFETY}
    cutoff = base.parse_utc(snapshot["knowledge_cutoff_utc"])
    if cutoff is None:
        return {"ok": False, "error": "snapshot_cutoff_invalid", **VIEW_SAFETY}
    raw_result = lake_snapshot.materialize_bars(
        snapshot, lake_root, adjustment="unadjusted", source_provider=source_provider)
    if not raw_result.get("ok"):
        return {"ok": False, "error": "raw_bar_materialization_failed",
                "detail": raw_result.get("error"), **VIEW_SAFETY}
    raw_snapshot = raw_result["bar_snapshot"]
    raw_bars = raw_snapshot["bars"]
    raw_lineage = {(row["symbol"], row["bar_date"]): row
                   for row in raw_snapshot["lineage"]}
    as_of = cutoff.date()
    contamination: list[str] = []
    for symbol, rows in raw_bars.items():
        for row in rows:
            bar_date = _date(row.get("date"))
            if bar_date is None or bar_date > as_of:
                contamination.append(f"bar_after_snapshot_cutoff:{symbol}:{row.get('date')}")
            lineage = raw_lineage.get((symbol, row.get("date")))
            observed = base.parse_utc((lineage or {}).get("observed_at_utc"))
            if observed is None or observed > cutoff:
                contamination.append(f"bar_observed_after_snapshot_cutoff:{symbol}:{row.get('date')}")
    if contamination:
        return {"ok": False, "error": "raw_bar_cutoff_contamination",
                "errors": sorted(set(contamination)), **VIEW_SAFETY}

    selected_actions: list[dict] = []
    action_lineage: list[dict] = []
    if view != "raw":
        selected_actions, action_lineage, action_errors = _collect_action_chains(
            snapshot, lake_root, policy, source_provider)
        if action_errors:
            return {"ok": False, "error": "corporate_action_chain_invalid",
                    "errors": action_errors, **VIEW_SAFETY}
    prepared_actions, action_errors = _prepare_actions(
        selected_actions, raw_bars, policy, view)
    if action_errors:
        return {"ok": False, "error": "corporate_action_factor_invalid",
                "errors": action_errors, **VIEW_SAFETY}
    prepared_by_hash = {item["record_sha256"]: item for item in prepared_actions}
    for entry in action_lineage:
        prepared = prepared_by_hash.get(entry["selected_record_sha256"])
        entry["applied_to_view"] = bool(prepared and prepared["applies_to_view"])

    places = policy["output_decimal_places"]
    quantum = Decimal(1).scaleb(-places)
    bars: dict[str, list[dict]] = {}
    bar_lineage: list[dict] = []
    roundtrip_checks = 0
    roundtrip_passed = 0
    action_applications = 0
    for symbol, rows in sorted(raw_bars.items()):
        for raw_row in rows:
            price_factor = Decimal("1")
            volume_factor = Decimal("1")
            applied: list[str] = []
            for action in prepared_actions:
                if (action["symbol"] == symbol and action["applies_to_view"]
                        and raw_row["date"] < action["effective_date"]):
                    price_factor *= action["price_factor"]
                    volume_factor *= action["volume_factor"]
                    applied.append(action["record_sha256"])
            action_applications += len(applied)
            output_row = {"date": raw_row["date"]}
            row_pass = True
            for field in ("open", "high", "low", "close"):
                raw_value = _decimal(raw_row.get(field), positive=True)
                if raw_value is None:
                    return {"ok": False, "error": "raw_bar_value_invalid",
                            "field": field, "symbol": symbol, "date": raw_row["date"],
                            **VIEW_SAFETY}
                output_value = (raw_row[field] if view == "raw"
                                else _round_decimal(raw_value * price_factor, places))
                output_row[field] = output_value
                roundtrip_checks += 1
                recovered = _decimal(output_value, positive=True) / price_factor
                tolerance = quantum * Decimal("2") / max(abs(price_factor), Decimal("0.0000001"))
                passed = abs(recovered - raw_value) <= tolerance
                roundtrip_passed += int(passed)
                row_pass = row_pass and passed
            if "volume" in raw_row:
                raw_volume = _decimal(raw_row.get("volume"), nonnegative=True)
                if raw_volume is None:
                    return {"ok": False, "error": "raw_bar_volume_invalid",
                            "symbol": symbol, "date": raw_row["date"], **VIEW_SAFETY}
                output_volume = (raw_row["volume"] if view == "raw"
                                 else _round_decimal(raw_volume * volume_factor, places))
                output_row["volume"] = output_volume
                roundtrip_checks += 1
                recovered_volume = _decimal(output_volume, nonnegative=True) / volume_factor
                tolerance = quantum * Decimal("2") / max(abs(volume_factor), Decimal("0.0000001"))
                passed = abs(recovered_volume - raw_volume) <= tolerance
                roundtrip_passed += int(passed)
                row_pass = row_pass and passed
            if view == "raw" and any(output_row.get(field) != raw_row.get(field)
                                     for field in output_row if field != "date"):
                return {"ok": False, "error": "raw_view_mutated_source_values", **VIEW_SAFETY}
            bars.setdefault(symbol, []).append(output_row)
            source = raw_lineage[(symbol, raw_row["date"])]
            bar_lineage.append({
                "symbol": symbol,
                "bar_date": raw_row["date"],
                "raw_record_sha256": source["record_sha256"],
                "raw_observed_at_utc": source["observed_at_utc"],
                "price_factor": str(price_factor),
                "volume_factor": str(volume_factor),
                "applied_action_record_sha256s": applied,
                "roundtrip_pass": row_pass,
            })
    if roundtrip_checks == 0 or roundtrip_passed != roundtrip_checks:
        return {"ok": False, "error": "adjustment_roundtrip_failed",
                "roundtrip_checks": roundtrip_checks,
                "roundtrip_passed": roundtrip_passed, **VIEW_SAFETY}
    quant_errors = features.validate_bars_archive({"bars": bars})
    policy_sha = base.sha256_obj(policy)
    bars_sha = base.sha256_obj(bars)
    lineage_payload = {"bars": bar_lineage, "actions": action_lineage}
    lineage_sha = base.sha256_obj(lineage_payload)
    counts = {
        "symbol_count": len(bars),
        "bar_count": sum(len(rows) for rows in bars.values()),
        "action_chain_count": len(action_lineage),
        "active_effective_action_count": sum(
            1 for row in action_lineage if row["state"] == "active_effective"),
        "rescinded_action_count": sum(1 for row in action_lineage if row["state"] == "rescinded"),
        "known_future_action_count": sum(
            1 for row in action_lineage if row["state"] == "known_future_not_effective"),
        "applied_action_count": sum(1 for row in action_lineage if row["applied_to_view"]),
        "bar_action_application_count": action_applications,
    }
    artifact = {
        "schema": VIEW_SCHEMA,
        "policy_version": policy["version"],
        "policy_sha256": policy_sha,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "knowledge_cutoff_utc": snapshot["knowledge_cutoff_utc"],
        "as_of_date": as_of.isoformat(),
        "view": view,
        "source_provider": source_provider,
        "bars": bars,
        "bar_lineage": bar_lineage,
        "action_lineage": action_lineage,
        "counts": counts,
        "roundtrip": {
            "check_count": roundtrip_checks,
            "pass_count": roundtrip_passed,
            "all_passed": roundtrip_passed == roundtrip_checks,
        },
        "quant_compatible": not quant_errors,
        "quant_validation_errors": quant_errors,
        "bars_payload_sha256": bars_sha,
        "lineage_sha256": lineage_sha,
        "dataset_id": "",
        **VIEW_SAFETY,
        "view_sha256": "",
    }
    artifact["dataset_id"] = base.sha256_obj(_view_identity(artifact))
    artifact["view_sha256"] = base.sha256_obj(_view_payload(artifact))
    errors = validate_view_artifact(artifact)
    if errors:
        return {"ok": False, "error": "view_self_validation_failed",
                "errors": errors, **VIEW_SAFETY}
    return {"ok": True, "artifact": artifact, **VIEW_SAFETY}


def validate_view_artifact(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["adjusted_view_not_object"]
    errors: list[str] = []
    if set(document) != _VIEW_TOP:
        errors.append("adjusted_view_top_level_keys_invalid")
    if document.get("schema") != VIEW_SCHEMA:
        errors.append("adjusted_view_schema_invalid")
    if not isinstance(document.get("policy_version"), str) or not _SEMVER_RE.fullmatch(
            str(document.get("policy_version") or "")):
        errors.append("adjusted_view_policy_version_invalid")
    for key in ("policy_sha256", "snapshot_id", "snapshot_sha256", "bars_payload_sha256",
                "lineage_sha256", "dataset_id", "view_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                str(document.get(key) or "")):
            errors.append(f"adjusted_view_hash_invalid:{key}")
    cutoff = base.parse_utc(document.get("knowledge_cutoff_utc"))
    if cutoff is None:
        errors.append("adjusted_view_cutoff_invalid")
    as_of = _date(document.get("as_of_date"))
    if as_of is None or (cutoff is not None and as_of != cutoff.date()):
        errors.append("adjusted_view_as_of_invalid")
    if document.get("view") not in SUPPORTED_VIEWS:
        errors.append("adjusted_view_name_invalid")
    if not isinstance(document.get("source_provider"), str) or not _TOKEN_RE.fullmatch(
            str(document.get("source_provider") or "")):
        errors.append("adjusted_view_source_provider_invalid")
    bars = document.get("bars")
    if not isinstance(bars, dict) or not bars:
        errors.append("adjusted_view_bars_invalid")
    bar_lineage = document.get("bar_lineage")
    action_lineage = document.get("action_lineage")
    if not isinstance(bar_lineage, list):
        errors.append("adjusted_view_bar_lineage_invalid")
        bar_lineage = []
    if not isinstance(action_lineage, list):
        errors.append("adjusted_view_action_lineage_invalid")
        action_lineage = []
    counts = document.get("counts")
    if not isinstance(counts, dict) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()):
        errors.append("adjusted_view_counts_invalid")
    roundtrip = document.get("roundtrip")
    if (not isinstance(roundtrip, dict)
            or set(roundtrip) != {"check_count", "pass_count", "all_passed"}
            or not isinstance(roundtrip.get("check_count"), int)
            or not isinstance(roundtrip.get("pass_count"), int)
            or roundtrip.get("check_count", 0) <= 0
            or roundtrip.get("pass_count") != roundtrip.get("check_count")
            or roundtrip.get("all_passed") is not True):
        errors.append("adjusted_view_roundtrip_invalid")
    quant_errors = document.get("quant_validation_errors")
    if not isinstance(quant_errors, list) or any(not isinstance(value, str) for value in quant_errors):
        errors.append("adjusted_view_quant_errors_invalid")
    if document.get("quant_compatible") is not (isinstance(quant_errors, list) and not quant_errors):
        errors.append("adjusted_view_quant_compatible_mismatch")
    for key, expected in VIEW_SAFETY.items():
        actual = document.get(key)
        if ((isinstance(expected, bool) and actual is not expected)
                or (not isinstance(expected, bool) and actual != expected)):
            errors.append(f"adjusted_view_safety_field_incorrect:{key}")
    if isinstance(bars, dict) and document.get("bars_payload_sha256") != base.sha256_obj(bars):
        errors.append("adjusted_view_bars_sha256_mismatch")
    lineage_payload = {"bars": bar_lineage, "actions": action_lineage}
    if document.get("lineage_sha256") != base.sha256_obj(lineage_payload):
        errors.append("adjusted_view_lineage_sha256_mismatch")
    if document.get("dataset_id") != base.sha256_obj(_view_identity(document)):
        errors.append("adjusted_view_dataset_id_mismatch")
    if document.get("view_sha256") != base.sha256_obj(_view_payload(document)):
        errors.append("adjusted_view_sha256_mismatch")
    return sorted(set(errors))


def verify_view(document: dict, snapshot: dict, lake_root: Path, policy: dict) -> dict:
    errors = validate_view_artifact(document)
    if errors:
        return {"ok": False, "error": "artifact_invalid", "errors": errors, **VIEW_SAFETY}
    rebuilt = materialize_view(snapshot, lake_root, policy=policy, view=document["view"],
                               source_provider=document["source_provider"])
    if not rebuilt.get("ok"):
        return {"ok": False, "error": "rebuild_failed", "detail": rebuilt.get("error"),
                **VIEW_SAFETY}
    expected = rebuilt["artifact"]
    match = base.canonical_bytes(expected) == base.canonical_bytes(document)
    return {
        "ok": match,
        "status": "match" if match else "drift",
        "artifact_view_sha256": document["view_sha256"],
        "rebuilt_view_sha256": expected["view_sha256"],
        "dataset_id": document["dataset_id"],
        **VIEW_SAFETY,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cutoff-aware bar adjustment views.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    materialize = sub.add_parser("materialize")
    materialize.add_argument("--policy", default=str(POLICY_DEFAULT))
    materialize.add_argument("--snapshot", required=True)
    materialize.add_argument("--lake-root", required=True)
    materialize.add_argument("--view", required=True, choices=SUPPORTED_VIEWS)
    materialize.add_argument("--source-provider", required=True)
    materialize.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--policy", default=str(POLICY_DEFAULT))
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--lake-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate":
            document = _read_json(Path(args.artifact))
            errors = validate_view_artifact(document)
            output = {"ok": not errors, "errors": errors,
                      "view_sha256": document.get("view_sha256"), **VIEW_SAFETY}
        else:
            policy, policy_errors = load_policy(Path(args.policy))
            if policy is None or policy_errors:
                output = {"ok": False, "error": "adjustment_policy_invalid",
                          "errors": policy_errors, **VIEW_SAFETY}
            else:
                snapshot = _read_json(Path(args.snapshot))
                if args.command == "materialize":
                    output = materialize_view(
                        snapshot, Path(args.lake_root), policy=policy, view=args.view,
                        source_provider=args.source_provider)
                    if output.get("ok"):
                        _atomic_write_once(Path(args.out), output["artifact"])
                        output["written"] = str(Path(args.out))
                else:
                    document = _read_json(Path(args.artifact))
                    output = verify_view(document, snapshot, Path(args.lake_root), policy)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "detail": str(exc)[:400], **VIEW_SAFETY}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
