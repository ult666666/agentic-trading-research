#!/usr/bin/env python3
"""Bounded, read-only power-source probe for the unattended trading window (2026-07-22).

Parses `pmset -g batt` only. Never mutates power settings, never needs sudo, never raises.
rc 0 = on AC power, 1 = ON BATTERY (unattended runs at risk: idle-sleep begins within ~1 minute
outside the continuity hold on this machine's live profile, and drain cost a full trading day on
2026-07-14), 2 = unknown/unavailable (fail-open for the caller: warn only on CONFIRMED battery).
Prints one JSON line: {"power_source": "AC"|"Battery"|"unknown", "percent": int|null}.
Stdlib-only, no model, no broker, no network. Origin: 2026-07-14 power-event postmortem.
"""
from __future__ import annotations

import json
import re
import subprocess

PMSET = "/usr/bin/pmset"


def probe() -> tuple[dict, int]:
    try:
        completed = subprocess.run(
            [PMSET, "-g", "batt"], capture_output=True, text=True, timeout=10)
        text = completed.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return {"power_source": "unknown", "percent": None}, 2
    source = "unknown"
    if "'AC Power'" in text:
        source = "AC"
    elif "'Battery Power'" in text:
        source = "Battery"
    match = re.search(r"(\d{1,3})%", text)
    percent = int(match.group(1)) if match else None
    document = {"power_source": source, "percent": percent}
    if source == "AC":
        return document, 0
    if source == "Battery":
        return document, 1
    return document, 2


def main() -> int:
    document, rc = probe()
    print(json.dumps(document, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
