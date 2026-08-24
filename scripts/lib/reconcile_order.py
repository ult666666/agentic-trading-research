#!/usr/bin/env python3
"""Compare intended, reviewed, submitted, and filled order records."""
from __future__ import annotations
import argparse, json, math
from pathlib import Path

CRITICAL_FIELDS = ("symbol", "side", "order_type")
NUMERIC_FIELDS = ("dollar_amount", "quantity", "limit_price")

def norm_text(v):
    return None if v is None else str(v).strip().upper()

def close(a, b, tolerance=1e-6):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return math.isclose(float(a), float(b), rel_tol=tolerance, abs_tol=tolerance)
    except Exception:
        return False

def compare_pair(label, left, right):
    mismatches = []
    for field in CRITICAL_FIELDS:
        if norm_text(left.get(field)) != norm_text(right.get(field)):
            mismatches.append({"stage": label, "field": field,
                               "expected": left.get(field), "actual": right.get(field)})
    for field in NUMERIC_FIELDS:
        if not close(left.get(field), right.get(field)):
            mismatches.append({"stage": label, "field": field,
                               "expected": left.get(field), "actual": right.get(field)})
    return mismatches

def reconcile(data):
    required = ("intent", "reviewed", "submitted")
    errors = [f"missing {x}" for x in required if not isinstance(data.get(x), dict)]
    if errors:
        return {"decision": "PAUSE", "matched": False, "errors": errors, "mismatches": []}
    mismatches = []
    mismatches += compare_pair("intent_vs_reviewed", data["intent"], data["reviewed"])
    mismatches += compare_pair("intent_vs_submitted", data["intent"], data["submitted"])
    status = str(data.get("status", "")).lower()
    if not status:
        mismatches.append({"stage": "status", "field": "status", "expected": "known", "actual": None})
    fill = data.get("fill")
    if fill is not None and not isinstance(fill, dict):
        mismatches.append({"stage": "fill", "field": "fill", "expected": "object_or_null", "actual": fill})
    return {
        "decision": "PAUSE" if mismatches else "CONTINUE_READ_ONLY",
        "matched": not mismatches,
        "mismatches": mismatches,
        "status": data.get("status"),
        "note": "This script never places, modifies, or cancels orders."
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("input")
    a = p.parse_args()
    try:
        data = json.loads(Path(a.input).read_text())
        result = reconcile(data)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["matched"] else 2
    except Exception as exc:
        print(json.dumps({"decision": "PAUSE", "matched": False, "errors": [str(exc)]}, indent=2))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
