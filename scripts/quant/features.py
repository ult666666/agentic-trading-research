#!/usr/bin/env python3
"""Point-in-time feature dataset builder for the offline quant lab.

The builder is deliberately isolated from production scoring and risk gates.  Its default
decision clock is ``prior_close``: for an event on session D, every market feature uses bars
strictly before D.  Event metadata must have been observed by a pre-open UTC knowledge cutoff.

Inputs
------
* Bars: ``{"bars": {"AAPL": [{"date": ..., "open": ..., ...}]}}``.
* Events: ``quant_event_snapshot/1.0`` with a UTC knowledge cutoff and timestamped events.

Outputs are deterministic, versioned, hash-chained diagnostics.  They never feed production,
never authorize a strategy change, and are excluded from every official performance metric.

Run::

    python3 scripts/quant/features.py build --bars data/replay_bars.json \
        --events /path/to/events.json --out /tmp/features.json
    python3 scripts/quant/features.py validate --path /tmp/features.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import DIAGNOSTIC_FIELDS  # noqa: E402
import validate_market_day  # noqa: E402

TOOL = "quant.features"
EVENT_SCHEMA = "quant_event_snapshot/1.0"
DATASET_SCHEMA = "quant_feature_dataset/1.0"
ROW_SCHEMA = "quant_feature_row/1.0"
DECISION_CLOCK = "prior_close"
BENCHMARK = "SPY"
MARKET_TZ = ZoneInfo("America/New_York")

SAFETY_FIELDS = {
    **DIAGNOSTIC_FIELDS,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "execution_enabled": False,
    "executed_trade_performance": False,
}

MARKET_FEATURES = (
    "prior_close",
    "return_1d",
    "return_5d",
    "return_20d",
    "sma_5",
    "sma_20",
    "sma_50",
    "distance_to_sma_5",
    "distance_to_sma_20",
    "distance_to_sma_50",
    "atr_14",
    "atr_pct_14",
    "volatility_20_annualized",
    "relative_strength_5d_vs_spy",
    "relative_strength_20d_vs_spy",
    "beta_60_vs_spy",
    "volume",
    "adv_20",
    "event_open_gap",
)
EVENT_FEATURES = (
    "spread_bps",
    "catalyst_type",
    "source_quality",
    "earnings_proximity_days",
    "dilution_risk",
    "macro_proximity_days",
    "sector_regime",
)
FEATURE_NAMES = MARKET_FEATURES + EVENT_FEATURES
NUMERIC_FEATURES = frozenset(MARKET_FEATURES[:-1]) | {
    "event_open_gap",
    "spread_bps",
    "earnings_proximity_days",
    "macro_proximity_days",
}
CORE_FEATURES = (
    "prior_close",
    "return_1d",
    "return_5d",
    "sma_5",
    "atr_pct_14",
    "relative_strength_5d_vs_spy",
)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)


def _base() -> dict:
    return {**SAFETY_FIELDS, "tool": TOOL}


def _refuse(error: str, **extra) -> dict:
    return {**_base(), "ok": False, "error": error, **extra}


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_obj(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _number(value, *, positive: bool = False, nonnegative: bool = False) -> bool:
    if not isinstance(value, _NUM) or isinstance(value, bool):
        return False
    if not math.isfinite(float(value)):
        return False
    if positive and value <= 0:
        return False
    if nonnegative and value < 0:
        return False
    return True


def _rounded(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    out = round(float(value), 10)
    return 0.0 if out == 0 else out


def _parse_date(value) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_utc(value) -> dt.datetime | None:
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    return parsed.astimezone(dt.timezone.utc)


def _market_open_utc(day: dt.date) -> dt.datetime:
    local = dt.datetime.combine(day, dt.time(9, 30), tzinfo=MARKET_TZ)
    return local.astimezone(dt.timezone.utc)


def _validate_event_feature(name: str, value) -> str | None:
    if value is None:
        return None
    if name == "spread_bps":
        return None if _number(value, nonnegative=True) else "must_be_nonnegative_number_or_null"
    if name in ("earnings_proximity_days", "macro_proximity_days"):
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        return None if ok else "must_be_nonnegative_integer_or_null"
    if name == "dilution_risk":
        ok = isinstance(value, bool) or (isinstance(value, str) and bool(value.strip()))
        return None if ok else "must_be_boolean_or_nonempty_string_or_null"
    ok = isinstance(value, str) and bool(value.strip())
    return None if ok else "must_be_nonempty_string_or_null"


def validate_event_snapshot(snapshot) -> list[str]:
    """Validate the versioned event snapshot independently of market bars."""
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["event_snapshot_not_object"]
    if snapshot.get("schema") != EVENT_SCHEMA:
        errors.append("event_snapshot_schema_mismatch")
    cutoff = _parse_utc(snapshot.get("knowledge_cutoff_utc"))
    if cutoff is None:
        errors.append("knowledge_cutoff_utc_invalid")
    events = snapshot.get("events")
    if not isinstance(events, list):
        return errors + ["events_not_list"]

    seen: set[str] = set()
    for i, event in enumerate(events):
        prefix = f"event[{i}]"
        if not isinstance(event, dict):
            errors.append(f"{prefix}.not_object")
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
            errors.append(f"{prefix}.event_id_invalid")
        elif event_id in seen:
            errors.append(f"{prefix}.event_id_duplicate:{event_id}")
        else:
            seen.add(event_id)
        symbol = event.get("symbol")
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            errors.append(f"{prefix}.symbol_invalid_or_not_uppercase")
        event_day = _parse_date(event.get("event_date"))
        if event_day is None:
            errors.append(f"{prefix}.event_date_invalid")
        observed = _parse_utc(event.get("observed_at_utc"))
        if observed is None:
            errors.append(f"{prefix}.observed_at_utc_invalid")
        if cutoff is not None and observed is not None and observed > cutoff:
            errors.append(f"{prefix}.observed_after_snapshot_cutoff")
        if cutoff is not None and event_day is not None and cutoff >= _market_open_utc(event_day):
            errors.append(f"{prefix}.snapshot_cutoff_not_pre_open")
        structured = event.get("structured_features", {})
        if not isinstance(structured, dict):
            errors.append(f"{prefix}.structured_features_not_object")
            continue
        unknown = sorted(set(structured) - set(EVENT_FEATURES))
        for name in unknown:
            errors.append(f"{prefix}.structured_feature_unknown:{name}")
        for name in EVENT_FEATURES:
            if name in structured:
                problem = _validate_event_feature(name, structured[name])
                if problem:
                    errors.append(f"{prefix}.structured_feature_{name}:{problem}")
    return errors


def _validate_bar(row, symbol: str, index: int) -> list[str]:
    prefix = f"bars.{symbol}[{index}]"
    if not isinstance(row, dict):
        return [f"{prefix}.not_object"]
    errors = []
    if _parse_date(row.get("date")) is None:
        errors.append(f"{prefix}.date_invalid")
    for field in ("open", "high", "low", "close"):
        if not _number(row.get(field), positive=True):
            errors.append(f"{prefix}.{field}_invalid")
    if not errors:
        high, low = float(row["high"]), float(row["low"])
        if high < max(float(row["open"]), float(row["close"]), low):
            errors.append(f"{prefix}.high_below_ohlc")
        if low > min(float(row["open"]), float(row["close"]), high):
            errors.append(f"{prefix}.low_above_ohlc")
    if "volume" in row and not _number(row.get("volume"), nonnegative=True):
        errors.append(f"{prefix}.volume_invalid")
    return errors


def validate_bars_archive(archive) -> list[str]:
    """Validate the replay archive shape, ordering, uniqueness, and values."""
    if not isinstance(archive, dict):
        return ["bars_archive_not_object"]
    bars = archive.get("bars")
    if not isinstance(bars, dict) or not bars:
        return ["bars_mapping_missing_or_empty"]
    errors: list[str] = []
    if BENCHMARK not in bars:
        errors.append("benchmark_SPY_missing")
    for symbol, rows in sorted(bars.items(), key=lambda item: str(item[0])):
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            errors.append(f"bars_symbol_invalid_or_not_uppercase:{symbol}")
            continue
        if not isinstance(rows, list) or not rows:
            errors.append(f"bars.{symbol}.rows_missing_or_empty")
            continue
        dates: list[str] = []
        for i, row in enumerate(rows):
            errors.extend(_validate_bar(row, symbol, i))
            if isinstance(row, dict) and isinstance(row.get("date"), str):
                dates.append(row["date"])
        if len(dates) != len(rows):
            continue
        for i in range(1, len(dates)):
            if dates[i] == dates[i - 1]:
                errors.append(f"bars.{symbol}.duplicate_or_conflicting_date:{dates[i]}")
            elif dates[i] < dates[i - 1]:
                errors.append(f"bars.{symbol}.dates_not_strictly_sorted:{dates[i]}")
    return errors


def _set_feature(features: dict, availability: dict, name: str, value,
                 reason: str | None, source: str) -> None:
    available = value is not None and reason is None
    features[name] = _rounded(value) if name in NUMERIC_FEATURES and available else value
    if not available:
        features[name] = None
    availability[name] = {
        "available": available,
        "reason": None if available else (reason or "unavailable_unspecified"),
        "source": source,
    }


def _period_return(rows: list[dict], periods: int) -> tuple[float | None, str | None]:
    if len(rows) < periods + 1:
        return None, f"insufficient_prior_bars_for_return_{periods}d"
    return float(rows[-1]["close"]) / float(rows[-1 - periods]["close"]) - 1.0, None


def _sma(rows: list[dict], length: int) -> tuple[float | None, str | None]:
    if len(rows) < length:
        return None, f"insufficient_prior_bars_for_sma_{length}"
    return statistics.fmean(float(r["close"]) for r in rows[-length:]), None


def _atr14(rows: list[dict]) -> tuple[float | None, str | None]:
    if len(rows) < 15:
        return None, "insufficient_prior_bars_for_atr_14"
    true_ranges = []
    for i in range(len(rows) - 14, len(rows)):
        row, previous = rows[i], rows[i - 1]
        high, low, previous_close = (
            float(row["high"]),
            float(row["low"]),
            float(previous["close"]),
        )
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return statistics.fmean(true_ranges), None


def _volatility20(rows: list[dict]) -> tuple[float | None, str | None]:
    if len(rows) < 21:
        return None, "insufficient_prior_bars_for_volatility_20"
    closes = [float(r["close"]) for r in rows[-21:]]
    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    return statistics.stdev(returns) * math.sqrt(252.0), None


def _relative_strength(rows: list[dict], benchmark_by_date: dict[str, dict], periods: int):
    if len(rows) < periods + 1:
        return None, f"insufficient_prior_bars_for_relative_strength_{periods}d"
    start, end = rows[-1 - periods], rows[-1]
    b0, b1 = benchmark_by_date.get(start["date"]), benchmark_by_date.get(end["date"])
    if b0 is None or b1 is None:
        return None, f"benchmark_missing_matching_dates_for_relative_strength_{periods}d"
    raw = float(end["close"]) / float(start["close"]) - 1.0
    bench = float(b1["close"]) / float(b0["close"]) - 1.0
    return raw - bench, None


def _beta60(rows: list[dict], benchmark_by_date: dict[str, dict]):
    aligned = [r for r in rows if r["date"] in benchmark_by_date]
    if len(aligned) < 61:
        return None, "insufficient_aligned_returns_for_beta_60", max(0, len(aligned) - 1)
    aligned = aligned[-61:]
    asset_returns = []
    benchmark_returns = []
    for previous, current in zip(aligned, aligned[1:]):
        b_previous = benchmark_by_date[previous["date"]]
        b_current = benchmark_by_date[current["date"]]
        asset_returns.append(float(current["close"]) / float(previous["close"]) - 1.0)
        benchmark_returns.append(
            float(b_current["close"]) / float(b_previous["close"]) - 1.0
        )
    mean_a = statistics.fmean(asset_returns)
    mean_b = statistics.fmean(benchmark_returns)
    covariance = sum((a - mean_a) * (b - mean_b)
                     for a, b in zip(asset_returns, benchmark_returns))
    variance = sum((b - mean_b) ** 2 for b in benchmark_returns)
    if variance <= 0:
        return None, "benchmark_variance_zero_for_beta_60", 60
    return covariance / variance, None, 60


def _event_feature_reason(event: dict, name: str) -> tuple[object | None, str | None]:
    structured = event.get("structured_features", {})
    if name not in structured:
        return None, "not_supplied_in_point_in_time_event_snapshot"
    if structured[name] is None:
        return None, "explicitly_unknown_in_event_snapshot"
    return structured[name], None


def _row_payload(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "row_sha256"}


def _dataset_payload(dataset: dict) -> dict:
    return {key: value for key, value in dataset.items() if key != "dataset_sha256"}


def _build_row(event: dict, rows: list[dict], benchmark_rows: list[dict],
               knowledge_cutoff: str) -> dict:
    event_date = event["event_date"]
    prior = [row for row in rows if row["date"] < event_date]
    benchmark_prior = [row for row in benchmark_rows if row["date"] < event_date]
    benchmark_by_date = {row["date"]: row for row in benchmark_prior}
    features: dict = {}
    availability: dict = {}

    if prior:
        _set_feature(features, availability, "prior_close", prior[-1]["close"], None,
                     "daily_bar_prior_close")
    else:
        _set_feature(features, availability, "prior_close", None,
                     "no_bar_strictly_before_event_date", "daily_bar_prior_close")

    for length in (1, 5, 20):
        value, reason = _period_return(prior, length)
        _set_feature(features, availability, f"return_{length}d", value, reason,
                     "daily_bar_prior_close")

    sma_values = {}
    for length in (5, 20, 50):
        value, reason = _sma(prior, length)
        sma_values[length] = value
        _set_feature(features, availability, f"sma_{length}", value, reason,
                     "daily_bar_prior_close")
        if value is None or not prior:
            distance = None
            distance_reason = reason or f"sma_{length}_unavailable"
        else:
            distance = float(prior[-1]["close"]) / value - 1.0
            distance_reason = None
        _set_feature(features, availability, f"distance_to_sma_{length}", distance,
                     distance_reason, "daily_bar_prior_close")

    atr, atr_reason = _atr14(prior)
    _set_feature(features, availability, "atr_14", atr, atr_reason, "daily_bar_prior_close")
    if atr is not None and prior:
        atr_pct, atr_pct_reason = atr / float(prior[-1]["close"]), None
    else:
        atr_pct, atr_pct_reason = None, atr_reason or "prior_close_unavailable"
    _set_feature(features, availability, "atr_pct_14", atr_pct, atr_pct_reason,
                 "daily_bar_prior_close")

    volatility, volatility_reason = _volatility20(prior)
    _set_feature(features, availability, "volatility_20_annualized", volatility,
                 volatility_reason, "daily_bar_prior_close")

    for length in (5, 20):
        value, reason = _relative_strength(prior, benchmark_by_date, length)
        _set_feature(features, availability, f"relative_strength_{length}d_vs_spy",
                     value, reason, "matched_daily_bars_prior_close")

    beta, beta_reason, aligned_return_count = _beta60(prior, benchmark_by_date)
    _set_feature(features, availability, "beta_60_vs_spy", beta, beta_reason,
                 "matched_daily_bars_prior_close")

    if prior and "volume" in prior[-1]:
        _set_feature(features, availability, "volume", prior[-1]["volume"], None,
                     "daily_bar_prior_close")
    else:
        _set_feature(features, availability, "volume", None,
                     "volume_unavailable_in_archive", "daily_bar_prior_close")
    if len(prior) >= 20 and all("volume" in row for row in prior[-20:]):
        adv = statistics.fmean(float(row["volume"]) for row in prior[-20:])
        _set_feature(features, availability, "adv_20", adv, None,
                     "daily_bar_prior_close")
    else:
        reason = ("volume_unavailable_in_archive" if not any("volume" in row for row in prior)
                  else "insufficient_valid_volume_bars_for_adv_20")
        _set_feature(features, availability, "adv_20", None, reason,
                     "daily_bar_prior_close")

    _set_feature(features, availability, "event_open_gap", None,
                 "not_available_at_prior_close", "decision_clock_policy")

    for name in EVENT_FEATURES:
        value, reason = _event_feature_reason(event, name)
        _set_feature(features, availability, name, value, reason,
                     "point_in_time_event_snapshot")

    exclusion_reasons = [
        f"missing_core_feature:{name}" for name in CORE_FEATURES
        if not availability[name]["available"]
    ]
    usable_cutoff = prior[-1]["date"] if prior else None
    benchmark_cutoff = benchmark_prior[-1]["date"] if benchmark_prior else None
    if usable_cutoff != benchmark_cutoff:
        exclusion_reasons.append("symbol_and_benchmark_prior_cutoffs_not_aligned")

    row = {
        **_base(),
        "schema": ROW_SCHEMA,
        "row_id": f"{event['event_id']}:{DECISION_CLOCK}",
        "event_id": event["event_id"],
        "symbol": event["symbol"],
        "event_date": event_date,
        "event_observed_at_utc": event["observed_at_utc"],
        "knowledge_cutoff_utc": knowledge_cutoff,
        "decision_clock": DECISION_CLOCK,
        "usable_bar_rule": "bar_date < event_date",
        "usable_bar_cutoff": usable_cutoff,
        "benchmark": BENCHMARK,
        "benchmark_bar_cutoff": benchmark_cutoff,
        "features": features,
        "feature_availability": availability,
        "lookback": {
            "symbol_prior_bars": len(prior),
            "benchmark_prior_bars": len(benchmark_prior),
            "aligned_prior_dates": sum(1 for row in prior if row["date"] in benchmark_by_date),
            "aligned_return_count_for_beta": aligned_return_count,
        },
        "prior_bar_lineage": {
            "symbol_bars_sha256": _sha256_obj(prior),
            "benchmark_bars_sha256": _sha256_obj(benchmark_prior),
            "contract": "exact_ordered_bars_with_date_strictly_before_event",
        },
        "core_features_required": list(CORE_FEATURES),
        "eligible_for_modeling": not exclusion_reasons,
        "exclusion_reasons": exclusion_reasons,
    }
    row["row_sha256"] = _sha256_obj(_row_payload(row))
    return row


def build_dataset(archive, snapshot, *, bars_file_sha256: str | None = None,
                  events_file_sha256: str | None = None) -> dict:
    """Build a deterministic point-in-time feature dataset or a labeled refusal."""
    event_errors = validate_event_snapshot(snapshot)
    if event_errors:
        return _refuse("invalid_event_snapshot", errors=event_errors)
    bar_errors = validate_bars_archive(archive)
    if bar_errors:
        return _refuse("invalid_bars_archive", errors=bar_errors)
    for label, value in (("bars_file_sha256", bars_file_sha256),
                         ("events_file_sha256", events_file_sha256)):
        if value is not None and (not isinstance(value, str) or not _SHA_RE.fullmatch(value)):
            return _refuse("invalid_external_file_hash", field=label)

    bars = archive["bars"]
    structural_errors = []
    for i, event in enumerate(snapshot["events"]):
        symbol = event["symbol"]
        if symbol not in bars:
            structural_errors.append(f"event[{i}].symbol_not_in_archive:{symbol}")
            continue
        event_date = event["event_date"]
        day = dt.date.fromisoformat(event_date)
        holidays, early = validate_market_day.load_calendar(validate_market_day.CONFIG)
        session = validate_market_day.verdict(day, holidays, early)
        if not session.get("is_trading_day"):
            structural_errors.append(f"event[{i}].event_date_not_trading_session:{event_date}")
        if not session.get("calendar_covers_year"):
            structural_errors.append(f"event[{i}].calendar_year_not_covered:{event_date}")
    if structural_errors:
        return _refuse("event_bar_alignment_invalid", errors=structural_errors)

    rows = [
        _build_row(event, bars[event["symbol"]], bars[BENCHMARK],
                   snapshot["knowledge_cutoff_utc"])
        for event in sorted(snapshot["events"], key=lambda item: (item["event_date"], item["event_id"]))
    ]
    dataset = {
        **_base(),
        "ok": True,
        "schema": DATASET_SCHEMA,
        "event_snapshot_schema": EVENT_SCHEMA,
        "row_schema": ROW_SCHEMA,
        "decision_clock": DECISION_CLOCK,
        "benchmark": BENCHMARK,
        "knowledge_cutoff_utc": snapshot["knowledge_cutoff_utc"],
        "feature_contract": {
            "bar_boundary": "strictly_before_event_date",
            "event_session_validation": "versioned market calendar; event-date bar not required",
            "returns": "close[-1] / close[-1-periods] - 1",
            "volatility_20": "sample_stdev(last_20_close_returns) * sqrt(252)",
            "relative_strength": "symbol_return - SPY_return_on_matching_dates",
            "beta_60": "covariance(symbol,SPY) / variance(SPY) over 60 aligned returns",
            "event_open_gap": "unavailable under prior_close decision clock",
            "core_features_required": list(CORE_FEATURES),
        },
        "lineage": {
            "bars_payload_sha256": _sha256_obj(archive),
            "events_payload_sha256": _sha256_obj(snapshot),
            "bars_file_sha256": bars_file_sha256,
            "events_file_sha256": events_file_sha256,
        },
        "row_count": len(rows),
        "eligible_row_count": sum(1 for row in rows if row["eligible_for_modeling"]),
        "rows": rows,
        "rows_payload_sha256": _sha256_obj(rows),
    }
    dataset["dataset_sha256"] = _sha256_obj(_dataset_payload(dataset))
    validation_errors = validate_dataset(dataset)
    if validation_errors:
        return _refuse("internal_dataset_validation_failed", errors=validation_errors)
    return dataset


def _safety_errors(obj: dict, prefix: str) -> list[str]:
    return [
        f"{prefix}.safety_field_incorrect:{key}"
        for key, expected in SAFETY_FIELDS.items()
        if obj.get(key) != expected
    ]


def validate_dataset(doc) -> list[str]:
    """Strictly validate hashes, temporal boundaries, safety flags, and feature nullability."""
    if not isinstance(doc, dict):
        return ["dataset_not_object"]
    errors = _safety_errors(doc, "dataset")
    if doc.get("ok") is not True:
        errors.append("dataset_ok_must_be_true")
    if doc.get("schema") != DATASET_SCHEMA:
        errors.append("dataset_schema_mismatch")
    if doc.get("event_snapshot_schema") != EVENT_SCHEMA:
        errors.append("event_snapshot_schema_mismatch")
    if doc.get("row_schema") != ROW_SCHEMA:
        errors.append("row_schema_mismatch")
    if doc.get("decision_clock") != DECISION_CLOCK:
        errors.append("decision_clock_mismatch")
    if doc.get("benchmark") != BENCHMARK:
        errors.append("benchmark_mismatch")
    cutoff = _parse_utc(doc.get("knowledge_cutoff_utc"))
    if cutoff is None:
        errors.append("dataset_knowledge_cutoff_invalid")

    lineage = doc.get("lineage")
    if not isinstance(lineage, dict):
        errors.append("lineage_missing_or_not_object")
    else:
        for key in ("bars_payload_sha256", "events_payload_sha256"):
            if not isinstance(lineage.get(key), str) or not _SHA_RE.fullmatch(lineage[key]):
                errors.append(f"lineage_hash_invalid:{key}")
        for key in ("bars_file_sha256", "events_file_sha256"):
            value = lineage.get(key)
            if value is not None and (not isinstance(value, str) or not _SHA_RE.fullmatch(value)):
                errors.append(f"lineage_hash_invalid:{key}")

    rows = doc.get("rows")
    if not isinstance(rows, list):
        return errors + ["rows_not_list"]
    if doc.get("row_count") != len(rows):
        errors.append("row_count_mismatch")
    if doc.get("eligible_row_count") != sum(
            1 for row in rows if isinstance(row, dict) and row.get("eligible_for_modeling") is True):
        errors.append("eligible_row_count_mismatch")

    seen_rows: set[str] = set()
    seen_events: set[str] = set()
    for i, row in enumerate(rows):
        prefix = f"row[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix}.not_object")
            continue
        errors.extend(_safety_errors(row, prefix))
        if row.get("schema") != ROW_SCHEMA:
            errors.append(f"{prefix}.schema_mismatch")
        row_id, event_id = row.get("row_id"), row.get("event_id")
        if not isinstance(row_id, str) or not row_id:
            errors.append(f"{prefix}.row_id_invalid")
        elif row_id in seen_rows:
            errors.append(f"{prefix}.row_id_duplicate:{row_id}")
        else:
            seen_rows.add(row_id)
        if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
            errors.append(f"{prefix}.event_id_invalid")
        elif event_id in seen_events:
            errors.append(f"{prefix}.event_id_duplicate:{event_id}")
        else:
            seen_events.add(event_id)
        if row_id != f"{event_id}:{DECISION_CLOCK}":
            errors.append(f"{prefix}.row_id_not_derived_from_event")
        if not isinstance(row.get("symbol"), str) or not _SYMBOL_RE.fullmatch(row["symbol"]):
            errors.append(f"{prefix}.symbol_invalid")
        event_day = _parse_date(row.get("event_date"))
        observed = _parse_utc(row.get("event_observed_at_utc"))
        row_cutoff = _parse_utc(row.get("knowledge_cutoff_utc"))
        if event_day is None:
            errors.append(f"{prefix}.event_date_invalid")
        if observed is None:
            errors.append(f"{prefix}.event_observed_at_invalid")
        if row_cutoff is None or row.get("knowledge_cutoff_utc") != doc.get("knowledge_cutoff_utc"):
            errors.append(f"{prefix}.knowledge_cutoff_invalid_or_mismatch")
        if observed is not None and row_cutoff is not None and observed > row_cutoff:
            errors.append(f"{prefix}.event_observed_after_cutoff")
        if event_day is not None and row_cutoff is not None and row_cutoff >= _market_open_utc(event_day):
            errors.append(f"{prefix}.knowledge_cutoff_not_pre_open")
        if row.get("decision_clock") != DECISION_CLOCK:
            errors.append(f"{prefix}.decision_clock_mismatch")
        if row.get("usable_bar_rule") != "bar_date < event_date":
            errors.append(f"{prefix}.usable_bar_rule_mismatch")
        usable = _parse_date(row.get("usable_bar_cutoff")) if row.get("usable_bar_cutoff") else None
        benchmark_cutoff = (_parse_date(row.get("benchmark_bar_cutoff"))
                            if row.get("benchmark_bar_cutoff") else None)
        if row.get("usable_bar_cutoff") is not None and usable is None:
            errors.append(f"{prefix}.usable_bar_cutoff_invalid")
        if usable is not None and event_day is not None and usable >= event_day:
            errors.append(f"{prefix}.future_bar_cutoff_violation")
        if benchmark_cutoff is not None and event_day is not None and benchmark_cutoff >= event_day:
            errors.append(f"{prefix}.benchmark_future_bar_cutoff_violation")
        if row.get("benchmark") != BENCHMARK:
            errors.append(f"{prefix}.benchmark_mismatch")

        features, availability = row.get("features"), row.get("feature_availability")
        if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
            errors.append(f"{prefix}.feature_keys_mismatch")
            features = features if isinstance(features, dict) else {}
        if not isinstance(availability, dict) or set(availability) != set(FEATURE_NAMES):
            errors.append(f"{prefix}.availability_keys_mismatch")
            availability = availability if isinstance(availability, dict) else {}
        for name in FEATURE_NAMES:
            state = availability.get(name)
            value = features.get(name)
            if not isinstance(state, dict):
                errors.append(f"{prefix}.{name}.availability_not_object")
                continue
            available = state.get("available")
            reason = state.get("reason")
            source = state.get("source")
            if not isinstance(available, bool):
                errors.append(f"{prefix}.{name}.available_not_boolean")
            if not isinstance(source, str) or not source:
                errors.append(f"{prefix}.{name}.source_missing")
            if available is True:
                if value is None:
                    errors.append(f"{prefix}.{name}.available_but_null")
                if reason is not None:
                    errors.append(f"{prefix}.{name}.available_but_reason_present")
                if name in NUMERIC_FEATURES and not _number(value):
                    errors.append(f"{prefix}.{name}.numeric_value_invalid")
                if name in ("catalyst_type", "source_quality", "sector_regime") and not (
                        isinstance(value, str) and value.strip()):
                    errors.append(f"{prefix}.{name}.categorical_value_invalid")
                if name == "dilution_risk" and not (
                        isinstance(value, bool) or (isinstance(value, str) and value.strip())):
                    errors.append(f"{prefix}.{name}.dilution_value_invalid")
            elif available is False:
                if value is not None:
                    errors.append(f"{prefix}.{name}.unavailable_but_value_present")
                if not isinstance(reason, str) or not reason:
                    errors.append(f"{prefix}.{name}.unavailable_reason_missing")
        gap_state = availability.get("event_open_gap", {})
        if (features.get("event_open_gap") is not None
                or gap_state.get("available") is not False
                or gap_state.get("reason") != "not_available_at_prior_close"):
            errors.append(f"{prefix}.event_open_gap_prior_close_contract_broken")

        prior_lineage = row.get("prior_bar_lineage")
        if not isinstance(prior_lineage, dict):
            errors.append(f"{prefix}.prior_bar_lineage_missing")
        else:
            for name in ("symbol_bars_sha256", "benchmark_bars_sha256"):
                digest = prior_lineage.get(name)
                if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
                    errors.append(f"{prefix}.prior_bar_lineage_hash_invalid:{name}")
            if prior_lineage.get("contract") != (
                    "exact_ordered_bars_with_date_strictly_before_event"):
                errors.append(f"{prefix}.prior_bar_lineage_contract_invalid")

        required = row.get("core_features_required")
        if required != list(CORE_FEATURES):
            errors.append(f"{prefix}.core_features_contract_mismatch")
        expected_missing = [
            f"missing_core_feature:{name}" for name in CORE_FEATURES
            if not isinstance(availability.get(name), dict)
            or availability[name].get("available") is not True
        ]
        exclusions = row.get("exclusion_reasons")
        if not isinstance(exclusions, list) or any(not isinstance(x, str) for x in exclusions):
            errors.append(f"{prefix}.exclusion_reasons_invalid")
            exclusions = []
        for expected in expected_missing:
            if expected not in exclusions:
                errors.append(f"{prefix}.missing_core_exclusion_absent:{expected}")
        expected_eligible = not exclusions
        if row.get("eligible_for_modeling") is not expected_eligible:
            errors.append(f"{prefix}.eligibility_inconsistent")

        expected_row_hash = _sha256_obj(_row_payload(row))
        if row.get("row_sha256") != expected_row_hash:
            errors.append(f"{prefix}.row_sha256_mismatch")

    if doc.get("rows_payload_sha256") != _sha256_obj(rows):
        errors.append("rows_payload_sha256_mismatch")
    expected_dataset_hash = _sha256_obj(_dataset_payload(doc))
    if doc.get("dataset_sha256") != expected_dataset_hash:
        errors.append("dataset_sha256_mismatch")
    contract = doc.get("feature_contract")
    if not isinstance(contract, dict) or contract.get("core_features_required") != list(CORE_FEATURES):
        errors.append("feature_contract_missing_or_weakened")
    return errors


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "file_missing"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"file_unreadable_or_malformed:{type(exc).__name__}"


def _atomic_write(path: Path, value: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_quant_features_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        return None
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _print(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time quant feature dataset (diagnostic only).")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--bars", required=True)
    build.add_argument("--events", required=True)
    build.add_argument("--out", default="-", help="output path, or - for stdout")
    build.add_argument("--replace", action="store_true",
                       help="atomically replace an existing output path")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    if args.command == "validate":
        path = Path(args.path)
        doc, problem = _read_json(path)
        if problem:
            _print(_refuse("dataset_read_failed", path=str(path), detail=problem))
            return 1
        errors = validate_dataset(doc)
        result = {**_base(), "ok": not errors, "errors": errors,
                  "path": str(path), "dataset_sha256": doc.get("dataset_sha256")}
        _print(result)
        return 0 if not errors else 1

    bars_path, events_path = Path(args.bars), Path(args.events)
    archive, bars_problem = _read_json(bars_path)
    snapshot, events_problem = _read_json(events_path)
    if bars_problem or events_problem:
        _print(_refuse("input_read_failed", bars_error=bars_problem,
                       events_error=events_problem))
        return 1
    result = build_dataset(
        archive,
        snapshot,
        bars_file_sha256=_sha256_file(bars_path),
        events_file_sha256=_sha256_file(events_path),
    )
    if result.get("ok") is not True:
        _print(result)
        return 1
    if args.out == "-":
        _print(result)
        return 0
    out_path = Path(args.out)
    try:
        problem = _atomic_write(out_path, result, args.replace)
    except OSError as exc:
        _print(_refuse("output_write_failed", path=str(out_path), detail=type(exc).__name__))
        return 1
    if problem:
        _print(_refuse(problem, path=str(out_path)))
        return 1
    _print({**_base(), "ok": True, "written": str(out_path),
            "dataset_sha256": result["dataset_sha256"], "row_count": result["row_count"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
