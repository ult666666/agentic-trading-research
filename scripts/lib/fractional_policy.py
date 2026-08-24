#!/usr/bin/env python3
"""Fractional-share capability policy.

The system must NEVER assert fractional/limit/order-type capability (or use it as a definitive
reason to reject a setup) unless it was VERIFIED via a read-only brokerage capability response
or current official broker documentation. Absent that, capability is 'unknown'.

Verified facts live in data/fractional_capability.json (written only after real verification).
"""
from __future__ import annotations
import json
from pathlib import Path

RECORD = Path(__file__).resolve().parent.parent.parent / "data" / "fractional_capability.json"
REQUIRED = ("supported_security_types", "whole_vs_fractional", "supported_order_types",
            "source", "verified_date")


def load_record() -> dict | None:
    try:
        return json.loads(RECORD.read_text())
    except Exception:
        return None


def capability() -> dict:
    """Return the verified capability, or an explicit 'unknown' that MUST NOT reject a setup."""
    rec = load_record()
    if not rec or rec.get("verified") is not True or not all(rec.get(k) for k in REQUIRED):
        return {
            "fractional_supported": "unknown",
            "order_types": "unknown",
            "verified": False,
            "may_be_used_to_reject_setup": False,
            "note": "UNVERIFIED — do not assert fractional/order-type limits; do not reject a setup on this basis.",
        }
    return {**rec, "fractional_supported": rec.get("whole_vs_fractional"),
            "may_be_used_to_reject_setup": True}


if __name__ == "__main__":
    print(json.dumps(capability(), indent=2, sort_keys=True))
