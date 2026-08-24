#!/usr/bin/env python3
"""Deterministic worker for the pre-registered catalyst strategy grid.

When and only when the point-in-time corpus is reviewable, this worker:

* materializes the complete pre-registered parameter grid;
* runs every variant through the conservative offline backtester;
* performs chronological train/validate/test selection with an embargo;
* locks the final calendar holdout before any variant is selected;
* computes DSR and CSCV/PBO on the complete pre-holdout trial matrix;
* pre-registers every variant's holdout portfolio and export contract;
* re-runs the final variant under higher transaction-cost assumptions;
* builds a normalized-capital candidate against a total-return SPY view; and
* atomically emits a ``quant_research_bundle`` consumed by research_factory.

The worker has no network, model, broker, production, or activation surface.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import artifact_provenance as provenance  # noqa: E402
import adjustment_policy as ap  # noqa: E402
import data_lake_ingest  # noqa: E402
import data_lake_intake  # noqa: E402
import data_lake_snapshot  # noqa: E402
from quant import backtest as qb  # noqa: E402
from quant import cost_model as cm  # noqa: E402
from quant import experiment_manifest as em  # noqa: E402
from quant import feature_corpus as fc  # noqa: E402
from quant import features as qf  # noqa: E402
from quant import model_diagnostics as qmd  # noqa: E402
from quant import portfolio_backtest as pb  # noqa: E402
from quant import research_factory as rf  # noqa: E402
from quant import selection_bias as sb  # noqa: E402
from quant import session_capture as sc  # noqa: E402
from quant import strategy_artifacts as sa  # noqa: E402
from quant import walk_forward as wf  # noqa: E402

TOOL = "quant.grid_worker"
SCHEMA = "quant_grid_worker/1.0"
STATUSES = {
    "data_not_ready", "corpus_unavailable", "corpus_not_reviewable",
    "insufficient_settled_sample", "insufficient_temporal_sample",
    "selection_bias_unavailable", "bundle_created", "bundle_existing", "blocked",
}
SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "network_invoked": False,
    "profitability_claim_allowed": False,
    "automatic_activation_allowed": False,
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _base() -> dict:
    return {**SAFETY, "tool": TOOL}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "worker_sha256"}


def _health(target: str, run_id: str) -> dict:
    return {
        **_base(),
        "ok": True,
        "schema": SCHEMA,
        "target_session_date": target,
        "post_close_run_id": run_id,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "blocked",
        "program_id": None,
        "corpus_sha256": None,
        "corpus_eligible_rows": 0,
        "settled_rows": 0,
        "pre_holdout_rows": 0,
        "holdout_rows": 0,
        "variant_count": 0,
        "fold_count": 0,
        "selected_variant_id": None,
        "bundle_path": None,
        "bundle_sha256": None,
        "portfolio_export_status": "not_attempted",
        "portfolio_export_contract_sha256": None,
        "candidate_portfolio_result_sha256": None,
        "benchmark_adjusted_view_sha256": None,
        "blockers": [],
        "exclusions": [],
        "activation_authority": False,
    }


def _finish(doc: dict) -> dict:
    doc["worker_sha256"] = _sha(_payload(doc))
    return doc


def validate_health(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["worker_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in SAFETY.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True or doc.get("schema") != SCHEMA:
        errors.append("worker_schema_or_ok_invalid")
    target = doc.get("target_session_date")
    if not isinstance(target, str) or not provenance.valid_postclose_run_id(
            doc.get("post_close_run_id"), target):
        errors.append("target_or_run_id_invalid")
    if provenance.parse_utc_strict(doc.get("generated_utc")) is None:
        errors.append("generated_utc_invalid")
    if doc.get("status") not in STATUSES:
        errors.append("status_invalid")
    for key in ("program_id", "selected_variant_id"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, str) or not _ID_RE.fullmatch(value)):
            errors.append(f"{key}_invalid")
    for key in ("corpus_sha256", "bundle_sha256"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, str) or not _SHA_RE.fullmatch(value)):
            errors.append(f"{key}_invalid")
    bundle_path = doc.get("bundle_path")
    if bundle_path is not None and (not isinstance(bundle_path, str) or not bundle_path):
        errors.append("bundle_path_invalid")
    for key in ("corpus_eligible_rows", "settled_rows", "pre_holdout_rows", "holdout_rows",
                "variant_count", "fold_count"):
        if not isinstance(doc.get(key), int) or isinstance(doc.get(key), bool) or doc[key] < 0:
            errors.append(f"count_invalid:{key}")
    for key in ("blockers", "exclusions"):
        if not isinstance(doc.get(key), list) or any(not isinstance(value, str)
                                                    for value in doc.get(key, [])):
            errors.append(f"{key}_invalid")
    export_status = doc.get("portfolio_export_status")
    if export_status not in {
            "not_attempted", "ready", "existing_ready"}:
        errors.append("portfolio_export_status_invalid")
    for key in ("portfolio_export_contract_sha256",
                "candidate_portfolio_result_sha256",
                "benchmark_adjusted_view_sha256"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, str)
                                  or not _SHA_RE.fullmatch(value)):
            errors.append(f"{key}_invalid")
    expected_export_status = {
        "bundle_created": "ready",
        "bundle_existing": "existing_ready",
    }.get(doc.get("status"))
    if expected_export_status is not None and export_status != expected_export_status:
        errors.append("bundle_status_export_status_mismatch")
    if export_status in {"ready", "existing_ready"}:
        for key in ("portfolio_export_contract_sha256",
                    "candidate_portfolio_result_sha256",
                    "benchmark_adjusted_view_sha256", "bundle_sha256"):
            if not isinstance(doc.get(key), str) or not _SHA_RE.fullmatch(doc[key]):
                errors.append(f"export_ready_missing:{key}")
        if not isinstance(doc.get("bundle_path"), str) or not doc["bundle_path"]:
            errors.append("export_ready_missing:bundle_path")
        if not isinstance(doc.get("selected_variant_id"), str) or not _ID_RE.fullmatch(
                doc["selected_variant_id"]):
            errors.append("export_ready_missing:selected_variant_id")
        if doc.get("blockers") != []:
            errors.append("export_ready_has_blockers")
    if doc.get("activation_authority") is not False:
        errors.append("activation_authority_invalid")
    if doc.get("worker_sha256") != _sha(_payload(doc)):
        errors.append("worker_sha256_mismatch")
    return sorted(set(errors))


def parameter_variants(program: dict) -> list[dict]:
    grid = program["parameter_grid"]
    keys = list(grid)
    variants = []
    for values in itertools.product(*(grid[key] for key in keys)):
        params = dict(zip(keys, values))
        variants.append({"variant_id": "v-" + _sha(params)[:16], "parameters": params})
    variants.sort(key=lambda value: value["variant_id"])
    if len(variants) != program["expected_trial_count"] or len({
            value["variant_id"] for value in variants}) != len(variants):
        raise ValueError("parameter_grid_count_or_identity_mismatch")
    return variants


def _subset_corpus(corpus: dict, event_ids: list[str]) -> dict:
    selected = set(event_ids)
    rows = [copy.deepcopy(row) for row in corpus["rows"] if row["event_id"] in selected]
    rows.sort(key=lambda row: (row["event_date"], row["event_id"]))
    child = copy.deepcopy(corpus)
    child["rows"] = rows
    child["row_count"] = len(rows)
    child["eligible_row_count"] = sum(row["eligible_for_modeling"] is True for row in rows)
    child["rows_sha256"] = fc._sha(rows)
    child["row_sources"] = {key: copy.deepcopy(value)
                            for key, value in corpus["row_sources"].items()
                            if key in selected}
    child["instrument_types"] = {key: value for key, value in corpus["instrument_types"].items()
                                 if key in selected}
    child["status"] = ("reviewable" if child["eligible_row_count"] >= fc.MIN_REVIEWABLE
                       else "accumulating" if rows else "empty")
    child["blockers"] = []
    child["corpus_sha256"] = fc._sha(fc._payload(child))
    errors = fc.validate_corpus(child)
    if errors:
        raise ValueError("subset_corpus_invalid:" + ",".join(errors))
    return child


def _settled_event_ids(corpus: dict, archive: dict, max_hold: int) -> tuple[list[str], list[str]]:
    settled, excluded = [], []
    bars = archive.get("bars", {})
    for row in sorted(corpus["rows"], key=lambda value: (value["event_date"], value["event_id"])):
        if row.get("eligible_for_modeling") is not True:
            excluded.append(f"ineligible_feature_row:{row['event_id']}")
            continue
        symbol_rows = bars.get(row["symbol"], [])
        dates = [value.get("date") for value in symbol_rows]
        if row["event_date"] not in dates:
            excluded.append(f"event_bar_missing:{row['event_id']}")
            continue
        index = dates.index(row["event_date"])
        if len(symbol_rows[index:index + max_hold]) < max_hold:
            excluded.append(f"outcome_window_pending:{row['event_id']}")
            continue
        settled.append(row["event_id"])
    return settled, excluded


def temporal_plan(rows: list[dict], program: dict, minimum_folds: int
                  ) -> tuple[dict | None, dict | None, list[str]]:
    ordered = sorted(rows, key=lambda value: (value["event_date"], value["event_id"]))
    if not ordered:
        return None, None, ["no_settled_rows"]
    max_date = dt.date.fromisoformat(ordered[-1]["event_date"])
    holdout_days = program["holdout_policy"]["holdout_calendar_days"]
    holdout_start = max_date - dt.timedelta(days=holdout_days - 1)
    embargo = program["walk_forward"]["embargo_calendar_days"]
    pre_end = holdout_start - dt.timedelta(days=embargo + 1)
    pre_rows = [row for row in ordered if dt.date.fromisoformat(row["event_date"]) <= pre_end]
    holdout_rows = [row for row in ordered
                    if dt.date.fromisoformat(row["event_date"]) >= holdout_start]
    gap_rows = [row for row in ordered if row not in pre_rows and row not in holdout_rows]
    if not pre_rows:
        return None, None, ["pre_holdout_sample_empty"]
    params = program["walk_forward"]
    walk = wf.build_folds(
        pre_rows[0]["event_date"], pre_end.isoformat(),
        params["train_calendar_days"], params["validate_calendar_days"],
        params["test_calendar_days"], params["embargo_calendar_days"],
        step_days=params["step_calendar_days"])
    if "error" in walk:
        return None, None, [f"walk_forward:{walk['error']}"]
    if walk["fold_count"] < minimum_folds:
        return None, None, [f"walk_forward_fold_count_lt_{minimum_folds}"]
    split = {
        "holdout_start": holdout_start.isoformat(),
        "pre_holdout_end": pre_end.isoformat(),
        "pre_event_ids": [row["event_id"] for row in pre_rows],
        "holdout_event_ids": [row["event_id"] for row in holdout_rows],
        "embargo_gap_event_ids": [row["event_id"] for row in gap_rows],
    }
    return walk, split, []


def _scenario_id(variant_id: str, event_id: str, cost: int) -> str:
    return f"s-{variant_id[2:10]}-{cost}-{_sha(event_id)[:16]}"


def build_plans(corpus: dict, variant: dict, program: dict, cost_bps: int,
                *, variant_suffix: str = "") -> dict:
    assumptions = program["simulation_assumptions"]
    params = variant["parameters"]
    variant_id = variant["variant_id"] + variant_suffix
    scenarios = []
    for row in corpus["rows"]:
        prior = row["features"].get("prior_close")
        atr = row["features"].get("atr_14")
        if not isinstance(prior, (int, float)) or not isinstance(atr, (int, float)) \
                or isinstance(prior, bool) or isinstance(atr, bool) or prior <= 0 or atr <= 0:
            raise ValueError(f"plan_feature_missing:{row['event_id']}")
        max_entry = float(prior) + float(params["max_entry_gap_atr"]) * float(atr)
        stop = float(prior) - float(params["stop_atr"]) * float(atr)
        target = max_entry + float(params["target_r_multiple"]) * (max_entry - stop)
        if not 0 < stop < max_entry < target:
            raise ValueError(f"plan_levels_invalid:{row['event_id']}")
        scenarios.append({
            "scenario_id": _scenario_id(variant_id, row["event_id"], cost_bps),
            "event_id": row["event_id"],
            "symbol": row["symbol"],
            "event_date": row["event_date"],
            "feature_row_sha256": row["row_sha256"],
            "specified_at_utc": row["knowledge_cutoff_utc"],
            "direction": "long",
            "instrument_type": corpus["instrument_types"][row["event_id"]],
            "entry_model": program["entry_model"],
            "max_entry": round(max_entry, 10),
            "stop_price": round(stop, 10),
            "target_price": round(target, 10),
            "max_hold_sessions": int(params["max_hold_sessions"]),
            "entry_slippage_bps": assumptions["entry_slippage_bps"],
            "exit_slippage_bps": assumptions["exit_slippage_bps"],
            "stop_slippage_bps": assumptions["stop_slippage_bps"],
            "transaction_cost_bps_per_side": cost_bps,
        })
    plans = {"schema": qb.PLAN_SCHEMA,
             "feature_dataset_sha256": corpus["corpus_sha256"],
             "variant_id": variant_id, "scenarios": scenarios,
             "parameters": copy.deepcopy(params), "cost_bps_per_side": cost_bps}
    errors = qb.validate_plans(plans, corpus)
    if errors:
        raise ValueError("generated_plans_invalid:" + ",".join(errors))
    return plans


def _artifact_ref(path: str, document: dict) -> dict:
    return {"path": path, "sha256": _sha(document)}


def _benchmark_source_provider(archive: dict, symbol: str) -> tuple[str | None, str | None]:
    configured = archive.get("source_provider_filter")
    if isinstance(configured, str) and configured:
        return configured, None
    providers = {
        row.get("source_provider") for row in archive.get("lineage", [])
        if isinstance(row, dict) and row.get("symbol") == symbol
        and isinstance(row.get("source_provider"), str) and row.get("source_provider")
    }
    if len(providers) != 1:
        return None, "benchmark_source_provider_not_unique"
    return next(iter(providers)), None


def _materialize_benchmark_view(snapshot: dict, lake_root: Path, archive: dict,
                                adjustment_policy: dict, symbol: str
                                ) -> tuple[dict | None, list[str]]:
    provider, problem = _benchmark_source_provider(archive, symbol)
    if problem:
        return None, [problem]
    result = ap.materialize_view(
        snapshot, lake_root, policy=adjustment_policy, view="total_return",
        source_provider=provider)
    if not result.get("ok"):
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        return None, [f"{result.get('error', 'unknown')}", *errors]
    artifact = result["artifact"]
    errors = ap.validate_view_artifact(artifact)
    if artifact.get("view") != "total_return":
        errors.append("benchmark_adjusted_view_not_total_return")
    if symbol not in artifact.get("bars", {}):
        errors.append("benchmark_symbol_missing")
    return (artifact, sorted(set(errors))) if errors else (artifact, [])


def build_portfolio_plan(corpus: dict, source_plans: dict, cost_policy: dict,
                         portfolio_policy: dict, export_policy: dict) -> dict:
    rows = {row["event_id"]: row for row in corpus["rows"]}
    ordered = sorted(source_plans["scenarios"], key=lambda value: (
        value["event_date"], value["event_id"], value["scenario_id"]))
    allocations = []
    for priority, scenario in enumerate(ordered, start=1):
        row = rows[scenario["event_id"]]
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        spread = features.get("spread_bps")
        if not isinstance(spread, (int, float)) or isinstance(spread, bool) or spread <= 0:
            spread = None
        annual_volatility = features.get("volatility_20_annualized")
        daily_volatility_bps = None
        if (isinstance(annual_volatility, (int, float))
                and not isinstance(annual_volatility, bool) and annual_volatility > 0):
            daily_volatility_bps = round(
                float(annual_volatility) * 10000.0 / math.sqrt(252.0), 10)
        catalyst = features.get("catalyst_type")
        if not isinstance(catalyst, str) or not pb._TOKEN_RE.fullmatch(catalyst):
            catalyst = "unknown_catalyst"
        allocations.append({
            "scenario_id": scenario["scenario_id"],
            "symbol": scenario["symbol"],
            "event_date": scenario["event_date"],
            "specified_at_utc": scenario["specified_at_utc"],
            "priority": priority,
            "requested_weight": float(export_policy["requested_weight"]),
            "sector": "unknown_sector",
            "catalyst_type": catalyst,
            "cost_context": {
                "observed_at_utc": scenario["specified_at_utc"],
                "decision_cutoff_utc": scenario["specified_at_utc"],
                "spread_bps": spread,
                "daily_volatility_bps": daily_volatility_bps,
                "fraction_of_adv": float(export_policy["assumed_fraction_of_adv"]),
                "source_artifact_sha256": row["row_sha256"],
            },
        })
    plan = {
        "schema": pb.PLAN_SCHEMA,
        "version": export_policy["version"],
        "source_backtest_plan_sha256": qb._sha256_obj(source_plans),
        "cost_policy_sha256": cm.sha256_obj(cost_policy),
        "portfolio_policy_sha256": pb.sha256_obj(portfolio_policy),
        "allocations": allocations,
        "safety": dict(pb.POLICY_SAFETY),
    }
    errors = pb.validate_plan(plan, source_plans, cost_policy, portfolio_policy)
    if errors:
        raise ValueError("generated_portfolio_plan_invalid:" + ",".join(errors))
    return plan


def _portfolio_source_hashes(backtest: dict, bars: dict, source_plans: dict,
                             portfolio_plan: dict, manifest: dict, cost_policy: dict,
                             portfolio_policy: dict) -> dict:
    return {
        "backtest_file_sha256": _sha(backtest),
        "bars_file_sha256": _sha(bars),
        "source_plans_file_sha256": _sha(source_plans),
        "portfolio_plan_file_sha256": _sha(portfolio_plan),
        "cost_policy_file_sha256": _sha(cost_policy),
        "portfolio_policy_file_sha256": _sha(portfolio_policy),
        "manifest_file_sha256": _sha(manifest),
    }


def _manifest(experiment_id: str, program: dict,
              preregistered_documents: dict[str, dict],
              created_utc: str) -> tuple[dict, dict]:
    source_hashes = {
        path: qb._sha256_obj(document)
        for path, document in sorted(preregistered_documents.items())
    }
    manifest = {
        **qf.DIAGNOSTIC_FIELDS,
        "manifest_version": 1,
        "experiment_id": experiment_id,
        "created_utc": created_utc,
        "git_commit": em.git_commit_or_none(),
        "data_snapshots": [{"path": name, "sha256": digest}
                           for name, digest in sorted(source_hashes.items())],
        "parameter_grid": copy.deepcopy(program["parameter_grid"]),
        "n_trials": program["expected_trial_count"],
        "multiple_testing_note": (
            "All pre-registered parameter variants are counted. Cost-stress plans are "
            "sensitivity checks of the selected rule, not additional selection trials."
        ),
        "selected_variants": [],
        "rejected_variants": [],
        "disclaimers": dict(em.REQUIRED_DISCLAIMERS),
    }
    errors = em.validate_manifest(manifest)
    if errors:
        raise ValueError("generated_manifest_invalid:" + ",".join(errors))
    return manifest, source_hashes


def _source_hashes(corpus: dict, archive: dict, plans: dict) -> dict:
    return {
        "features_file_sha256": qb._sha256_obj(corpus),
        "bars_file_sha256": qb._sha256_obj(archive),
        "plans_file_sha256": qb._sha256_obj(plans),
    }


def _returns(result: dict, event_ids: list[str]) -> list[float] | None:
    wanted = set(event_ids)
    records = [record for record in result["records"] if record["event_id"] in wanted]
    if len(records) != len(wanted):
        return None
    values = []
    for record in records:
        if record["simulation_status"] == "completed":
            values.append(float(record["net_return_after_modeled_costs"]))
        elif record["simulation_status"] == "no_fill":
            values.append(0.0)
        else:
            return None
    return values


def _score(result: dict, event_ids: list[str]) -> tuple[float, float] | None:
    values = _returns(result, event_ids)
    if values is None or not values:
        return None
    mean = statistics.fmean(values)
    sharpe = -math.inf
    if len(values) >= 2:
        stdev = statistics.stdev(values)
        if stdev > 0:
            sharpe = mean / stdev
    return sharpe, mean


def select_variant(results: dict[str, dict], train_ids: list[str], validate_ids: list[str],
                   shortlist_size: int) -> tuple[str | None, dict]:
    train_scores = {variant_id: _score(result, train_ids)
                    for variant_id, result in results.items()}
    train_scores = {key: value for key, value in train_scores.items() if value is not None}
    ranked = sorted(train_scores, key=lambda key: (
        -train_scores[key][0], -train_scores[key][1], key))
    shortlist = ranked[:shortlist_size]
    validation_scores = {variant_id: _score(results[variant_id], validate_ids)
                         for variant_id in shortlist}
    validation_scores = {key: value for key, value in validation_scores.items()
                         if value is not None}
    if not validation_scores:
        return None, {"reason": "validation_scores_unavailable"}
    selected = sorted(validation_scores, key=lambda key: (
        -validation_scores[key][0], -validation_scores[key][1], key))[0]
    return selected, {
        "train_shortlist": shortlist,
        "selected_variant_id": selected,
        "train_score": list(train_scores[selected]),
        "validation_score": list(validation_scores[selected]),
    }


def _ids_in_range(rows: list[dict], span: dict) -> list[str]:
    return [row["event_id"] for row in rows
            if span["start"] <= row["event_date"] <= span["end"]]


def _scenario_ids(result: dict, event_ids: list[str]) -> list[str]:
    wanted = set(event_ids)
    values = [record["scenario_id"] for record in result["records"]
              if record["event_id"] in wanted]
    if len(values) != len(wanted):
        raise ValueError("scenario_event_mapping_not_one_to_one")
    return sorted(values)


def _write_json(root: Path, relative: str, doc: dict) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(doc))
    return {"path": relative, "sha256": _sha_file(path)}


def _write_bundle(experiment_root: Path, experiment_id: str, documents: dict,
                  bundle_parts: dict, artifact_policy: dict
                  ) -> tuple[str, str, str | None]:
    experiment_root.mkdir(parents=True, exist_ok=True)
    destination = experiment_root / experiment_id
    if destination.exists():
        bundle, problem = fc._read(destination / "bundle.json")
        if problem or not isinstance(bundle, dict):
            return str(destination), "", "existing_bundle_unreadable"
        return str(destination), str(bundle.get("bundle_sha256") or ""), None
    temporary = Path(tempfile.mkdtemp(prefix=f".{experiment_id}.tmp-",
                                      dir=str(experiment_root)))
    try:
        refs = {name: _write_json(temporary, relative, doc)
                for name, (relative, doc) in documents.items()}
        bundle = {
            **qf.SAFETY_FIELDS,
            "schema": rf.BUNDLE_SCHEMA,
            "bundle_id": experiment_id,
            "experiment_id": experiment_id,
            "program_id": bundle_parts["program_id"],
            "program_version": bundle_parts["program_version"],
            "preregistered_at_utc": bundle_parts["preregistered_at_utc"],
            "holdout_locked_at_utc": bundle_parts["holdout_locked_at_utc"],
            "selection_completed_at_utc": bundle_parts["selection_completed_at_utc"],
            "manifest": refs["manifest"],
            "walk_forward": refs["walk_forward"],
            "selection_bias": refs["selection_bias"],
            "folds": [{"fold": value["fold"],
                       "backtest": refs[f"fold_{value['fold']}_backtest"],
                       "diagnostics": refs[f"fold_{value['fold']}_diagnostics"]}
                      for value in bundle_parts["folds"]],
            "holdout": {"backtest": refs["holdout_backtest"],
                        "diagnostics": refs["holdout_diagnostics"]},
            "cost_stress": [{"name": value["name"],
                             "cost_multiplier": value["cost_multiplier"],
                             "backtest": refs[value["backtest_key"]],
                             "diagnostics": refs[value["diagnostics_key"]]}
                            for value in bundle_parts["cost_stress"]],
            "portfolio_export": {
                "contract": refs[bundle_parts["portfolio_export"]["contract_key"]],
                "candidate_portfolio": refs[
                    bundle_parts["portfolio_export"]["candidate_key"]],
                "source_backtest": refs[
                    bundle_parts["portfolio_export"]["source_backtest_key"]],
            },
        }
        bundle["bundle_sha256"] = rf._sha256_obj(rf._bundle_payload(bundle))
        _write_json(temporary, "bundle.json", bundle)
        preflight = sa.inspect_factory_bundle(
            temporary / "bundle.json", experiment_root, artifact_policy)
        if preflight.get("status") != "ready":
            shutil.rmtree(temporary, ignore_errors=True)
            return (str(destination), "",
                    f"strategy_export_preflight:{preflight.get('status') or preflight.get('error')}")
        os.replace(temporary, destination)
        return str(destination), bundle["bundle_sha256"], None
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable_or_malformed:{type(exc).__name__}"


def run_worker(*, target: str, run_id: str, readiness_path: Path, corpus_path: Path,
               config_path: Path, data_config_path: Path, experiment_root: Path,
               adjustment_policy_path: Path = ap.POLICY_DEFAULT,
               cost_policy_path: Path = cm.POLICY_DEFAULT,
               portfolio_policy_path: Path = pb.POLICY_DEFAULT,
               artifact_policy_path: Path = sa.POLICY_DEFAULT,
               ) -> tuple[dict, int]:
    health = _health(target, run_id)
    if not provenance.valid_postclose_run_id(run_id, target):
        health["blockers"] = ["run_id_invalid_for_target"]
        return _finish(health), 1
    config, config_errors = rf.load_config(config_path)
    if config is None or config_errors:
        health["blockers"] = [f"research_config:{value}" for value in config_errors]
        return _finish(health), 1
    program = config["programs"][0]
    health["program_id"] = program["program_id"]
    receipt, problem = _load_json(readiness_path)
    if problem:
        health["blockers"] = [f"readiness:{problem}"]
        return _finish(health), 1
    receipt_errors = data_lake_intake.validate_receipt(receipt)
    if receipt_errors or receipt.get("target_session_date") != target:
        health["blockers"] = [f"readiness:{value}" for value in receipt_errors]
        if receipt.get("target_session_date") != target:
            health["blockers"].append("readiness_target_mismatch")
        return _finish(health), 1
    if receipt.get("research_ready") is not True:
        health["status"] = "data_not_ready"
        quality = receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {}
        health["blockers"] = sorted(set((quality.get("readiness_blockers") or [])
                                        + (quality.get("warnings") or [])))
        return _finish(health), 0
    corpus, problem = _load_json(corpus_path)
    if problem:
        health["status"] = "corpus_unavailable"
        health["blockers"] = [f"feature_corpus:{problem}"]
        return _finish(health), 0
    corpus_errors = fc.validate_corpus(corpus)
    if corpus_errors:
        health["blockers"] = [f"feature_corpus:{value}" for value in corpus_errors]
        return _finish(health), 1
    health["corpus_sha256"] = corpus["corpus_sha256"]
    health["corpus_eligible_rows"] = corpus["eligible_row_count"]
    if corpus.get("status") != "reviewable":
        health["status"] = "corpus_not_reviewable"
        health["blockers"] = [f"corpus_status:{corpus.get('status')}"]
        return _finish(health), 0
    data_config, data_errors = data_lake_ingest.load_config(data_config_path)
    if data_config is None or data_errors:
        health["blockers"] = [f"data_config:{value}" for value in data_errors]
        return _finish(health), 1
    snapshot, lake_root, _instruments, snapshot_errors = sc._snapshot_and_instruments(
        receipt, data_config, target)
    if snapshot is None or lake_root is None:
        health["blockers"] = snapshot_errors
        return _finish(health), 1
    bars_result = data_lake_snapshot.materialize_bars(snapshot, lake_root, adjustment="adjusted")
    if not bars_result.get("ok"):
        health["blockers"] = [f"bar_materialization:{bars_result.get('error', 'unknown')}"]
        return _finish(health), 1
    archive = bars_result["bar_snapshot"]
    max_hold = max(program["parameter_grid"]["max_hold_sessions"])
    settled_ids, exclusions = _settled_event_ids(corpus, archive, max_hold)
    health["settled_rows"] = len(settled_ids)
    health["exclusions"] = exclusions
    minimum_total = (config["evidence_gates"]["minimum_completed_oos"]
                     + config["evidence_gates"]["minimum_holdout_completed"])
    if len(settled_ids) < minimum_total:
        health["status"] = "insufficient_settled_sample"
        health["blockers"] = [f"settled_rows_lt_{minimum_total}"]
        return _finish(health), 0
    evaluation = _subset_corpus(corpus, settled_ids)
    rows = evaluation["rows"]
    walk, split, split_errors = temporal_plan(
        rows, program, config["evidence_gates"]["minimum_walk_forward_folds"])
    if split_errors:
        health["status"] = "insufficient_temporal_sample"
        health["blockers"] = split_errors
        return _finish(health), 0
    health["pre_holdout_rows"] = len(split["pre_event_ids"])
    health["holdout_rows"] = len(split["holdout_event_ids"])
    health["fold_count"] = walk["fold_count"]
    if len(split["holdout_event_ids"]) < config["evidence_gates"]["minimum_holdout_completed"]:
        health["status"] = "insufficient_temporal_sample"
        health["blockers"] = ["holdout_observation_count_insufficient"]
        return _finish(health), 0
    for fold in walk["folds"]:
        counts = {name: len(_ids_in_range(rows, fold[name]))
                  for name in ("train", "validate", "test")}
        if counts["train"] < 2 or counts["validate"] < 2 or counts["test"] < 1:
            health["status"] = "insufficient_temporal_sample"
            health["blockers"] = [f"fold_{fold['fold']}_event_density_insufficient"]
            return _finish(health), 0

    adjustment_policy, adjustment_errors = ap.load_policy(adjustment_policy_path)
    cost_policy, cost_errors = cm.load_policy(cost_policy_path)
    portfolio_policy, portfolio_errors = pb.load_policy(portfolio_policy_path)
    artifact_policy, artifact_errors = sa.load_policy(artifact_policy_path)
    policy_errors = (
        [f"adjustment_policy:{value}" for value in adjustment_errors]
        + [f"cost_policy:{value}" for value in cost_errors]
        + [f"portfolio_policy:{value}" for value in portfolio_errors]
        + [f"artifact_policy:{value}" for value in artifact_errors]
    )
    if any(value is None for value in (
            adjustment_policy, cost_policy, portfolio_policy, artifact_policy)):
        policy_errors.append("portfolio_export_policy_document_missing")
    if policy_errors:
        health["blockers"] = sorted(set(policy_errors))
        return _finish(health), 1
    export_policy = program["portfolio_export_policy"]
    compatibility_errors = []
    if export_policy["benchmark_symbol"] != artifact_policy["benchmark_symbol"]:
        compatibility_errors.append("benchmark_symbol_policy_mismatch")
    if export_policy["benchmark_adjustment_view"] != artifact_policy[
            "benchmark_adjustment_view"]:
        compatibility_errors.append("benchmark_adjustment_view_policy_mismatch")
    if float(export_policy["cost_stress_multiplier"]) not in [
            float(value) for value in cost_policy["stress_multipliers"]]:
        compatibility_errors.append("portfolio_export_cost_stress_not_allowlisted")
    if float(export_policy["requested_weight"]) > float(
            portfolio_policy["maximum_position_weight"]):
        compatibility_errors.append("portfolio_export_weight_above_position_cap")
    if float(export_policy["assumed_fraction_of_adv"]) > float(
            cost_policy["maximum_fraction_of_adv"]):
        compatibility_errors.append("portfolio_export_adv_fraction_above_cost_cap")
    if compatibility_errors:
        health["blockers"] = compatibility_errors
        return _finish(health), 1
    benchmark_view, benchmark_errors = _materialize_benchmark_view(
        snapshot, lake_root, archive, adjustment_policy,
        export_policy["benchmark_symbol"])
    if benchmark_view is None or benchmark_errors:
        health["status"] = "data_not_ready"
        health["blockers"] = [f"benchmark_adjusted_view:{value}"
                              for value in benchmark_errors]
        return _finish(health), 0
    health["benchmark_adjusted_view_sha256"] = benchmark_view["view_sha256"]
    holdout_evaluation = _subset_corpus(evaluation, split["holdout_event_ids"])

    variants = parameter_variants(program)
    health["variant_count"] = len(variants)
    base_cost = program["simulation_assumptions"]["base_transaction_cost_bps_per_side"]
    base_plans: dict[str, dict] = {}
    stress_plans: dict[tuple[str, int], dict] = {}
    holdout_base_plans: dict[str, dict] = {}
    holdout_stress_plans: dict[tuple[str, int], dict] = {}
    portfolio_plans: dict[str, dict] = {}
    preregistered_documents: dict[str, dict] = {
        "inputs/corpus.json": evaluation,
        "inputs/holdout_corpus.json": holdout_evaluation,
        "inputs/bars.json": archive,
        "inputs/cost_policy.json": cost_policy,
        "inputs/portfolio_policy.json": portfolio_policy,
        "inputs/benchmark_total_return_view.json": benchmark_view,
    }
    for variant in variants:
        variant_id = variant["variant_id"]
        base_plan = build_plans(evaluation, variant, program, base_cost)
        holdout_plan = build_plans(holdout_evaluation, variant, program, base_cost)
        portfolio_plan = build_portfolio_plan(
            holdout_evaluation, holdout_plan, cost_policy, portfolio_policy,
            export_policy)
        base_plans[variant_id] = base_plan
        holdout_base_plans[variant_id] = holdout_plan
        portfolio_plans[variant_id] = portfolio_plan
        preregistered_documents[f"variants/{variant_id}/plans.json"] = base_plan
        preregistered_documents[f"variants/{variant_id}/holdout_plans.json"] = holdout_plan
        preregistered_documents[f"variants/{variant_id}/portfolio_plan.json"] = portfolio_plan
        for cost in program["modeled_costs_bps_per_side"][1:]:
            suffix = f"-cost-{cost}"
            plan = build_plans(evaluation, variant, program, cost, variant_suffix=suffix)
            holdout_stress = build_plans(
                holdout_evaluation, variant, program, cost, variant_suffix=suffix)
            stress_plans[(variant_id, cost)] = plan
            holdout_stress_plans[(variant_id, cost)] = holdout_stress
            preregistered_documents[
                f"variants/{variant_id}/plans-cost-{cost}.json"] = plan
            preregistered_documents[
                f"variants/{variant_id}/holdout_plans-cost-{cost}.json"] = holdout_stress

    input_identity = {
        "program_id": program["program_id"], "program_version": program["program_version"],
        "evaluation_corpus_sha256": evaluation["corpus_sha256"],
        "archive_sha256": qb._sha256_obj(archive),
        "benchmark_adjusted_view_sha256": benchmark_view["view_sha256"],
        "cost_policy_sha256": cm.sha256_obj(cost_policy),
        "portfolio_policy_sha256": pb.sha256_obj(portfolio_policy),
        "artifact_policy_sha256": sa.sha256_obj(artifact_policy),
        "portfolio_export_policy": export_policy,
        "preregistered_inputs_sha256": _sha({
            path: _sha(document)
            for path, document in sorted(preregistered_documents.items())
        }),
        "split": split,
    }
    experiment_id = program["program_id"] + "-" + _sha(input_identity)[:20]
    destination = experiment_root / experiment_id
    if destination.exists():
        existing, problem = _load_json(destination / "bundle.json")
        if problem or rf.validate_bundle_structure(existing):
            health["blockers"] = ["existing_bundle_invalid"]
            return _finish(health), 1
        preflight = sa.inspect_factory_bundle(
            destination / "bundle.json", experiment_root, artifact_policy)
        if preflight.get("status") != "ready":
            health["blockers"] = [
                f"existing_bundle_strategy_export:{preflight.get('status') or preflight.get('error')}"]
            return _finish(health), 1
        contract_path = destination / existing["portfolio_export"]["contract"]["path"]
        candidate_path = destination / existing[
            "portfolio_export"]["candidate_portfolio"]["path"]
        contract, contract_problem = _load_json(contract_path)
        candidate, candidate_problem = _load_json(candidate_path)
        if contract_problem or candidate_problem:
            health["blockers"] = ["existing_bundle_export_artifact_unreadable"]
            return _finish(health), 1
        health.update({"status": "bundle_existing", "bundle_path": str(destination),
                       "bundle_sha256": existing["bundle_sha256"],
                       "portfolio_export_status": "existing_ready",
                       "portfolio_export_contract_sha256": contract["contract_sha256"],
                       "candidate_portfolio_result_sha256": candidate["result_sha256"]})
        return _finish(health), 0

    preregistered = dt.datetime.now(dt.timezone.utc).isoformat()
    export_contracts: dict[str, dict] = {}
    for variant in variants:
        variant_id = variant["variant_id"]
        input_refs = {
            "source_bars": _artifact_ref("inputs/bars.json", archive),
            "source_plans": _artifact_ref(
                f"variants/{variant_id}/holdout_plans.json",
                holdout_base_plans[variant_id]),
            "portfolio_plan": _artifact_ref(
                f"variants/{variant_id}/portfolio_plan.json",
                portfolio_plans[variant_id]),
            "cost_policy": _artifact_ref("inputs/cost_policy.json", cost_policy),
            "portfolio_policy": _artifact_ref(
                "inputs/portfolio_policy.json", portfolio_policy),
            "benchmark_adjusted_view": _artifact_ref(
                "inputs/benchmark_total_return_view.json", benchmark_view),
        }
        contract = sa.build_export_contract(
            experiment_id, program["program_id"], program["program_version"],
            preregistered, float(export_policy["cost_stress_multiplier"]),
            input_refs, artifact_policy)
        contract_errors = sa.validate_export_contract(contract, artifact_policy)
        if contract_errors:
            health["blockers"] = [f"export_contract:{value}"
                                  for value in contract_errors]
            return _finish(health), 1
        export_contracts[variant_id] = contract
        preregistered_documents[
            f"variants/{variant_id}/portfolio_export_contract.json"] = contract
    manifest, _all_hashes = _manifest(
        experiment_id, program, preregistered_documents, preregistered)
    results: dict[str, dict] = {}
    for variant in variants:
        variant_id = variant["variant_id"]
        plan = base_plans[variant_id]
        result = qb.run_backtest(evaluation, archive, plan, manifest,
                                 _source_hashes(evaluation, archive, plan))
        if result.get("ok") is not True:
            health["blockers"] = [f"backtest_failed:{variant_id}:{result.get('error')}"]
            return _finish(health), 1
        if result["summary"]["pending_count"] or result["summary"]["excluded_count"]:
            health["blockers"] = [f"backtest_unsettled_or_excluded:{variant_id}"]
            return _finish(health), 1
        results[variant_id] = result

    fold_artifacts = []
    selection_records = []
    selected_test_returns: dict[str, list[float]] = defaultdict(list)
    shortlist_size = program["selection_policy"]["train_shortlist_size"]
    for fold in walk["folds"]:
        train_ids = _ids_in_range(rows, fold["train"])
        validation_ids = _ids_in_range(rows, fold["validate"])
        test_ids = _ids_in_range(rows, fold["test"])
        selected, selection = select_variant(results, train_ids, validation_ids,
                                             shortlist_size)
        if selected is None:
            health["blockers"] = [f"fold_selection_failed:{fold['fold']}"]
            return _finish(health), 1
        subset = qb.subset_result(results[selected],
                                  _scenario_ids(results[selected], test_ids),
                                  f"fold-{fold['fold']}-test")
        diagnostics = qmd.analyze(subset)
        if subset.get("ok") is not True or diagnostics.get("ok") is not True:
            health["blockers"] = [f"fold_artifact_failed:{fold['fold']}"]
            return _finish(health), 1
        selected_test_returns[selected].extend(_returns(results[selected], test_ids) or [])
        selection_records.append({"fold": fold["fold"], **selection})
        fold_artifacts.append({"fold": fold["fold"], "backtest": subset,
                               "diagnostics": diagnostics})

    counts = Counter(value["selected_variant_id"] for value in selection_records)
    max_count = max(counts.values())
    finalists = [variant_id for variant_id, count in counts.items() if count == max_count]
    final_variant = sorted(finalists, key=lambda variant_id: (
        -statistics.fmean(selected_test_returns[variant_id])
        if selected_test_returns[variant_id] else math.inf,
        variant_id))[0]
    health["selected_variant_id"] = final_variant

    slices = program["selection_bias_policy"]["cscv_slices"]
    pre_rows = [row for row in rows if row["event_id"] in set(split["pre_event_ids"])]
    pre_rows.sort(key=lambda value: (value["event_date"], value["event_id"]))
    remainder = len(pre_rows) % slices
    selection_ids = [row["event_id"] for row in pre_rows[remainder:]]
    if len(selection_ids) < program["selection_bias_policy"]["minimum_observations"]:
        health["status"] = "selection_bias_unavailable"
        health["blockers"] = ["selection_matrix_observation_count_insufficient"]
        return _finish(health), 0
    selection_results = {
        variant_id: qb.subset_result(
            result, _scenario_ids(result, selection_ids), "selection-bias-sample")
        for variant_id, result in results.items()
    }
    matrix = sb.build_matrix_from_results(
        experiment_id, selection_results, final_variant, program["expected_trial_count"])
    if matrix.get("ok") is not True:
        health["status"] = "selection_bias_unavailable"
        health["blockers"] = [f"selection_matrix:{matrix.get('error')}"]
        return _finish(health), 0
    selection_bias = sb.analyze_matrix(matrix, slices=slices)
    if selection_bias.get("ok") is not True:
        health["status"] = "selection_bias_unavailable"
        health["blockers"] = [f"selection_bias:{selection_bias.get('error')}"]
        return _finish(health), 0

    holdout_plan = holdout_base_plans[final_variant]
    holdout_result = qb.run_backtest(
        holdout_evaluation, archive, holdout_plan, manifest,
        _source_hashes(holdout_evaluation, archive, holdout_plan))
    holdout_diagnostics = qmd.analyze(holdout_result)
    if holdout_result.get("ok") is not True or holdout_diagnostics.get("ok") is not True:
        health["blockers"] = [
            f"holdout_artifact:{holdout_result.get('error') or holdout_diagnostics.get('error') or 'failed'}",
            *[f"holdout_artifact:{value}" for value in holdout_result.get("errors", [])],
            *[f"holdout_diagnostics:{value}"
              for value in holdout_diagnostics.get("errors", [])],
        ]
        return _finish(health), 1
    if holdout_result["summary"]["pending_count"] or holdout_result[
            "summary"]["excluded_count"]:
        health["blockers"] = ["holdout_unsettled_or_excluded"]
        return _finish(health), 1

    portfolio_plan = portfolio_plans[final_variant]
    candidate_portfolio = pb.run_portfolio(
        holdout_result, archive, holdout_plan, portfolio_plan, manifest,
        cost_policy, portfolio_policy,
        _portfolio_source_hashes(
            holdout_result, archive, holdout_plan, portfolio_plan, manifest,
            cost_policy, portfolio_policy),
        float(export_policy["cost_stress_multiplier"]))
    if candidate_portfolio.get("ok") is not True:
        health["blockers"] = [
            f"candidate_portfolio:{candidate_portfolio.get('error', 'unknown')}",
            *[f"candidate_portfolio:{value}"
              for value in candidate_portfolio.get("errors", [])],
        ]
        return _finish(health), 1
    health["candidate_portfolio_result_sha256"] = candidate_portfolio["result_sha256"]
    health["portfolio_export_contract_sha256"] = export_contracts[
        final_variant]["contract_sha256"]

    stress_artifacts = []
    for cost in program["modeled_costs_bps_per_side"][1:]:
        plan = holdout_stress_plans[(final_variant, cost)]
        result = qb.run_backtest(
            holdout_evaluation, archive, plan, manifest,
            _source_hashes(holdout_evaluation, archive, plan))
        diagnostics = qmd.analyze(result) if result.get("ok") else result
        if result.get("ok") is not True or diagnostics.get("ok") is not True:
            health["blockers"] = [f"cost_stress_failed:{cost}"]
            return _finish(health), 1
        if result["summary"]["pending_count"] or result["summary"]["excluded_count"]:
            health["blockers"] = [f"cost_stress_unsettled_or_excluded:{cost}"]
            return _finish(health), 1
        stress_artifacts.append({"cost": cost, "backtest": result,
                                 "diagnostics": diagnostics})

    selection_completed = dt.datetime.now(dt.timezone.utc).isoformat()
    documents = {
        "manifest": ("manifest.json", manifest),
        "walk_forward": ("walk_forward.json", walk),
        "selection_matrix": ("selection_matrix.json", matrix),
        "selection_bias": ("selection_bias.json", selection_bias),
        "selection_record": ("selection_record.json", {
            **qf.SAFETY_FIELDS, "experiment_id": experiment_id,
            "fold_selections": selection_records, "final_variant_id": final_variant,
            "final_variant_rule": program["selection_policy"]["final_variant_rule"],
            "selection_sample_dropped_oldest_count": remainder,
        }),
        "holdout_backtest": ("holdout/backtest.json", holdout_result),
        "holdout_diagnostics": ("holdout/diagnostics.json", holdout_diagnostics),
        "candidate_portfolio": ("holdout/candidate_portfolio.json", candidate_portfolio),
    }
    preregistered_keys = {}
    for index, (path, document) in enumerate(sorted(preregistered_documents.items())):
        key = f"preregistered_{index:04d}"
        preregistered_keys[path] = key
        documents[key] = (path, document)
    for variant in variants:
        variant_id = variant["variant_id"]
        documents[f"variant_{variant_id}_backtest"] = (
            f"variants/{variant_id}/backtest.json", results[variant_id])
    for value in fold_artifacts:
        documents[f"fold_{value['fold']}_backtest"] = (
            f"folds/fold-{value['fold']}/backtest.json", value["backtest"])
        documents[f"fold_{value['fold']}_diagnostics"] = (
            f"folds/fold-{value['fold']}/diagnostics.json", value["diagnostics"])
    stress_parts = []
    for value in stress_artifacts:
        cost = value["cost"]
        backtest_key = f"cost_{cost}_backtest"
        diagnostics_key = f"cost_{cost}_diagnostics"
        documents[backtest_key] = (f"cost_stress/cost-{cost}/backtest.json",
                                   value["backtest"])
        documents[diagnostics_key] = (f"cost_stress/cost-{cost}/diagnostics.json",
                                      value["diagnostics"])
        stress_parts.append({
            "name": f"transaction_cost_{cost}bps_per_side",
            "cost_multiplier": cost / base_cost,
            "backtest_key": backtest_key,
            "diagnostics_key": diagnostics_key,
        })
    bundle_parts = {
        "program_id": program["program_id"],
        "program_version": program["program_version"],
        "preregistered_at_utc": preregistered,
        "holdout_locked_at_utc": preregistered,
        "selection_completed_at_utc": selection_completed,
        "folds": fold_artifacts,
        "cost_stress": stress_parts,
        "portfolio_export": {
            "contract_key": preregistered_keys[
                f"variants/{final_variant}/portfolio_export_contract.json"],
            "candidate_key": "candidate_portfolio",
            "source_backtest_key": "holdout_backtest",
        },
    }
    bundle_path, bundle_sha, bundle_error = _write_bundle(
        experiment_root, experiment_id, documents, bundle_parts, artifact_policy)
    if bundle_error:
        health["blockers"] = [bundle_error]
        return _finish(health), 1
    health.update({"status": "bundle_created", "bundle_path": bundle_path,
                   "bundle_sha256": bundle_sha,
                   "portfolio_export_status": "ready"})
    return _finish(health), 0


def _atomic_write(path: Path, doc: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_grid_worker_",
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
    parser = argparse.ArgumentParser(description="Run the pre-registered offline strategy grid.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--target", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--readiness", required=True)
    run.add_argument("--corpus", required=True)
    run.add_argument("--config", default=str(rf.CONFIG_DEFAULT))
    run.add_argument("--data-config", default=str(data_lake_ingest.CONFIG_DEFAULT))
    run.add_argument("--adjustment-policy", default=str(ap.POLICY_DEFAULT))
    run.add_argument("--cost-policy", default=str(cm.POLICY_DEFAULT))
    run.add_argument("--portfolio-policy", default=str(pb.POLICY_DEFAULT))
    run.add_argument("--artifact-policy", default=str(sa.POLICY_DEFAULT))
    run.add_argument("--experiment-root", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        doc, problem = _load_json(Path(args.path))
        if problem:
            print(json.dumps({**_base(), "ok": False, "error": problem}, indent=2))
            return 1
        errors = validate_health(doc)
        print(json.dumps({**_base(), "ok": not errors, "errors": errors},
                         indent=2, sort_keys=True))
        return 0 if not errors else 1
    doc, rc = run_worker(
        target=args.target, run_id=args.run_id, readiness_path=Path(args.readiness),
        corpus_path=Path(args.corpus), config_path=Path(args.config),
        data_config_path=Path(args.data_config), experiment_root=Path(args.experiment_root),
        adjustment_policy_path=Path(args.adjustment_policy),
        cost_policy_path=Path(args.cost_policy),
        portfolio_policy_path=Path(args.portfolio_policy),
        artifact_policy_path=Path(args.artifact_policy))
    write_error = _atomic_write(Path(args.out), doc, args.replace)
    if write_error:
        print(json.dumps({**_base(), "ok": False, "error": write_error}, indent=2))
        return 1
    print(json.dumps({**_base(), "ok": rc == 0, "status": doc["status"],
                      "bundle_path": doc["bundle_path"], "written": args.out,
                      "worker_sha256": doc["worker_sha256"]}, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
