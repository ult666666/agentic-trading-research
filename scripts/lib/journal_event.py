#!/usr/bin/env python3
"""Append one structured event to a JSONL trading journal."""
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path

FORBIDDEN_KEYS = {"password", "token", "cookie", "secret", "account_number", "api_key", "mfa"}

def contains_forbidden(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS or contains_forbidden(v):
                return True
    elif isinstance(obj, list):
        return any(contains_forbidden(x) for x in obj)
    return False

def main():
    p = argparse.ArgumentParser()
    p.add_argument("event")
    p.add_argument("--journal", default="trading_journal.jsonl")
    a = p.parse_args()
    try:
        event = json.loads(Path(a.event).read_text())
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        if contains_forbidden(event):
            raise ValueError("event contains a forbidden secret-like field")
        event.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
        path = Path(a.journal)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        print(json.dumps({"appended": True, "journal": str(path)}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"appended": False, "error": str(exc)}, indent=2))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
