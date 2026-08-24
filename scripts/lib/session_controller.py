#!/usr/bin/env python3
"""Persistent fail-closed trading-session state machine."""
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path

STATES = {"SETUP", "READ_ONLY", "AWAITING_CONFIRMATION", "ACTIVE_ORDER",
          "PAUSED", "EXIT_REVIEW", "STOPPED", "CLOSED"}

TRANSITIONS = {
    "SETUP": {"READ_ONLY", "STOPPED"},
    "READ_ONLY": {"AWAITING_CONFIRMATION", "PAUSED", "STOPPED", "CLOSED"},
    "AWAITING_CONFIRMATION": {"READ_ONLY", "ACTIVE_ORDER", "PAUSED", "STOPPED"},
    "ACTIVE_ORDER": {"READ_ONLY", "PAUSED", "EXIT_REVIEW", "STOPPED"},
    "PAUSED": {"READ_ONLY", "EXIT_REVIEW", "STOPPED", "CLOSED"},
    "EXIT_REVIEW": {"AWAITING_CONFIRMATION", "PAUSED", "STOPPED", "CLOSED"},
    "STOPPED": {"CLOSED"},
    "CLOSED": set(),
}

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"state": "READ_ONLY", "session_date": datetime.now(timezone.utc).date().isoformat(),
                "history": [], "reason": "initialized"}
    try:
        data = json.loads(path.read_text())
        if data.get("state") not in STATES or not isinstance(data.get("history", []), list):
            raise ValueError("invalid state file")
        return data
    except Exception:
        return {"state": "STOPPED", "session_date": datetime.now(timezone.utc).date().isoformat(),
                "history": [], "reason": "corrupt_or_unreadable_state"}

def transition(path: Path, target: str, reason: str) -> dict:
    data = load_state(path)
    current = data["state"]
    target = target.upper()
    ok = target in TRANSITIONS.get(current, set())
    event = {"timestamp": datetime.now(timezone.utc).isoformat(),
             "from": current, "to": target, "reason": reason, "accepted": ok}
    data.setdefault("history", []).append(event)
    if ok:
        data["state"] = target
        data["reason"] = reason
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return {"accepted": ok, "current_state": data["state"], "event": event}

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default="trading_state.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    t = sub.add_parser("transition")
    t.add_argument("target", choices=sorted(STATES))
    t.add_argument("--reason", required=True)
    a = p.parse_args()
    path = Path(a.file)
    if a.command == "show":
        print(json.dumps(load_state(path), indent=2, sort_keys=True))
        return 0
    result = transition(path, a.target, a.reason)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
