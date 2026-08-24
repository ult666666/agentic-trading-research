#!/usr/bin/env python3
"""Deterministic candidate score calculator."""
from __future__ import annotations
import argparse, json
from pathlib import Path

MAXIMA = {
    "catalyst": 25,
    "trend_relative_strength": 20,
    "liquidity_volume": 15,
    "entry_quality": 15,
    "reward_risk": 15,
    "data_confidence": 10,
}

def score(data: dict) -> dict:
    errors = []
    components = {}
    for key, maximum in MAXIMA.items():
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
            errors.append(f"{key} must be an integer from 0 to {maximum}")
        else:
            components[key] = value
    hard_rejects = [str(x) for x in data.get("hard_rejects", []) if str(x).strip()]
    total = sum(components.values()) if not errors else 0
    threshold = data.get("threshold", 78)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 <= threshold <= 100:
        errors.append("threshold must be an integer from 0 to 100")
        threshold = 78
    eligible = not errors and not hard_rejects and total >= threshold
    return {
        "score": total,
        "threshold": threshold,
        "eligible_for_risk_review": eligible,
        "components": components,
        "hard_rejects": hard_rejects,
        "errors": errors,
    }

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input")
    a = p.parse_args()
    try:
        data = json.loads(Path(a.input).read_text())
        print(json.dumps(score(data), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"score": 0, "eligible_for_risk_review": False, "errors": [str(exc)]}, indent=2))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
