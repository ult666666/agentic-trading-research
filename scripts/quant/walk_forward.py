#!/usr/bin/env python3
"""Offline quant lab — WALK-FORWARD SPLITTER (Layer 3 skeleton). DIAGNOSTIC ONLY.

Safety contract:
  * Research lab output. Never feeds production scoring/gates; every result (including
    refusals) self-labels with the decision_path diagnostic-exclusion fields via
    quant.DIAGNOSTIC_FIELDS so it can never be counted as strategy performance.
  * Fail-closed: malformed dates, non-positive window lengths, negative embargo, a step
    that would overlap test windows, or a span too small for one complete fold =>
    {"error": ...} + exit 1. No partial fold lists.
  * Leak refusal: within each fold train < embargo gap < validate < embargo gap < test in
    strict calendar order; across folds the out-of-sample TEST windows are pairwise
    disjoint (each date is tested at most once — multiple-testing discipline). Every
    built plan is re-verified by an independent checker before it is returned; a checker
    failure is a refusal, never a silent pass.
  * Pure calendar-date math (std-lib datetime only). This module deliberately knows
    nothing about bars, prices, or trading calendars: mapping calendar spans onto actual
    trading days is the caller's job and must itself be point-in-time.

Fold layout (all ranges inclusive, ISO dates):
    train  = [S + k*step,               S + k*step + train_days - 1]
    validate starts embargo_days AFTER train ends (gap of exactly embargo_days days).
    test     starts embargo_days AFTER validate ends.
Default step = test_days, so consecutive test windows tile without overlap.

Run: python3 scripts/quant/walk_forward.py --start 2026-01-01 --end 2026-06-30 \
        --train-days 60 --validate-days 10 --test-days 10 --embargo-days 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import DIAGNOSTIC_FIELDS  # noqa: E402

TOOL = "quant.walk_forward"
_DAY = dt.timedelta(days=1)


def _base() -> dict:
    return {**DIAGNOSTIC_FIELDS, "tool": TOOL}


def _refuse(err, **extra):
    return {**_base(), "error": err, **extra}


def _parse_date(s):
    if not isinstance(s, str):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _span(d0: dt.date, d1: dt.date) -> dict:
    return {"start": d0.isoformat(), "end": d1.isoformat(), "days": (d1 - d0).days + 1}


def verify_folds(folds, embargo_days):
    """Independent re-check of a built plan (fail-closed second opinion).

    Confirms: per-fold internal ordering with an embargo gap of >= embargo_days calendar
    days between train->validate and validate->test, well-formed inclusive ranges, and
    pairwise-disjoint test windows across folds. Returns {"ok": bool, "violations": []}.
    """
    violations = []
    parsed = []
    for f in folds:
        try:
            tr0, tr1 = _parse_date(f["train"]["start"]), _parse_date(f["train"]["end"])
            va0, va1 = _parse_date(f["validate"]["start"]), _parse_date(f["validate"]["end"])
            te0, te1 = _parse_date(f["test"]["start"]), _parse_date(f["test"]["end"])
        except (TypeError, KeyError):
            violations.append({"fold": f.get("fold") if isinstance(f, dict) else None,
                               "violation": "malformed_fold_record"})
            continue
        if None in (tr0, tr1, va0, va1, te0, te1):
            violations.append({"fold": f.get("fold"), "violation": "malformed_fold_dates"})
            continue
        if not (tr0 <= tr1 and va0 <= va1 and te0 <= te1):
            violations.append({"fold": f.get("fold"), "violation": "inverted_range"})
        # Inclusive ranges: the gap between train end and validate start is
        # (va0 - tr1).days - 1 full calendar days; it must cover the embargo.
        if (va0 - tr1).days - 1 < embargo_days or va0 <= tr1:
            violations.append({"fold": f.get("fold"),
                               "violation": "train_validate_embargo_violated"})
        if (te0 - va1).days - 1 < embargo_days or te0 <= va1:
            violations.append({"fold": f.get("fold"),
                               "violation": "validate_test_embargo_violated"})
        parsed.append((f.get("fold"), te0, te1))
    parsed.sort(key=lambda x: x[1])
    for (fa, a0, a1), (fb, b0, b1) in zip(parsed, parsed[1:]):
        if b0 <= a1:
            violations.append({"fold": fb, "violation": "test_window_overlaps_previous_fold",
                               "overlaps_fold": fa})
    return {"ok": not violations, "violations": violations}


def build_folds(start, end, train_days, validate_days, test_days, embargo_days,
                step_days=None, max_folds=None):
    """Build time-ordered rolling train/validate/test folds. Refusals carry "error"."""
    d0, d1 = _parse_date(start), _parse_date(end)
    if d0 is None or d1 is None:
        return _refuse("malformed_date", start=start, end=end)
    if d1 < d0:
        return _refuse("end_before_start", start=start, end=end)
    for name, v in (("train_days", train_days), ("validate_days", validate_days),
                    ("test_days", test_days)):
        if not _pos_int(v):
            return _refuse("invalid_window_length", parameter=name, value=v)
    if not isinstance(embargo_days, int) or isinstance(embargo_days, bool) or embargo_days < 0:
        return _refuse("invalid_embargo_days", value=embargo_days)
    if step_days is None:
        step_days = test_days
    if not _pos_int(step_days):
        return _refuse("invalid_step_days", value=step_days)
    if step_days < test_days:
        # A short step re-tests dates already consumed by the previous fold's test
        # window: overlapping out-of-sample windows double-count outcomes (leaky).
        return _refuse("overlapping_test_windows", step_days=step_days, test_days=test_days,
                       required="step_days >= test_days")
    if max_folds is not None and not _pos_int(max_folds):
        return _refuse("invalid_max_folds", value=max_folds)

    folds = []
    k = 0
    while True:
        tr0 = d0 + dt.timedelta(days=k * step_days)
        tr1 = tr0 + dt.timedelta(days=train_days - 1)
        va0 = tr1 + dt.timedelta(days=embargo_days + 1)
        va1 = va0 + dt.timedelta(days=validate_days - 1)
        te0 = va1 + dt.timedelta(days=embargo_days + 1)
        te1 = te0 + dt.timedelta(days=test_days - 1)
        if te1 > d1:
            break
        folds.append({"fold": len(folds) + 1, "train": _span(tr0, tr1),
                      "validate": _span(va0, va1), "test": _span(te0, te1)})
        k += 1
        if max_folds is not None and len(folds) >= max_folds:
            break
    if not folds:
        return _refuse("insufficient_span_for_one_fold",
                       span_days=(d1 - d0).days + 1,
                       required_days=train_days + validate_days + test_days
                       + 2 * embargo_days)

    verification = verify_folds(folds, embargo_days)
    if not verification["ok"]:
        # Unreachable by construction; kept as a fail-closed second opinion.
        return _refuse("fold_verification_failed", verification=verification)

    return {**_base(), "start": d0.isoformat(), "end": d1.isoformat(),
            "params": {"train_days": train_days, "validate_days": validate_days,
                       "test_days": test_days, "embargo_days": embargo_days,
                       "step_days": step_days, "max_folds": max_folds},
            "date_convention": "inclusive calendar-date ranges; caller maps to trading days",
            "fold_count": len(folds), "folds": folds, "verification": verification}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Time-ordered rolling train/validate/test splits with embargo "
                    "(research lab only; pure date math).")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD first usable date")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD last usable date (inclusive)")
    ap.add_argument("--train-days", required=True, type=int)
    ap.add_argument("--validate-days", required=True, type=int)
    ap.add_argument("--test-days", required=True, type=int)
    ap.add_argument("--embargo-days", required=True, type=int,
                    help="calendar days excluded between train/validate and validate/test")
    ap.add_argument("--step-days", type=int, default=None,
                    help="fold advance (default: test-days; must be >= test-days)")
    ap.add_argument("--max-folds", type=int, default=None)
    args = ap.parse_args(argv)

    result = build_folds(args.start, args.end, args.train_days, args.validate_days,
                         args.test_days, args.embargo_days, step_days=args.step_days,
                         max_folds=args.max_folds)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
