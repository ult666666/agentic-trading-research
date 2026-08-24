#!/usr/bin/env python3
"""Structured-candidate adoption ROLLUP over per-run health artifacts (read-only, fail-closed).

One deterministic command that answers: across recent morning runs, is the structured candidate
path consistently working, or is it falling back / breaking? Reads ONLY the derived per-run
artifacts written by scripts/candidate_artifact_health.py:

    data/runtime/health/<YYYY-MM-DD>_candidate_artifact_health.json

It reads no broker state, no ledger, no report, and never modifies any input it reads; with
--out it writes EXACTLY ONE derived artifact (its own rollup JSON, atomic tempfile+os.replace,
best-effort — a write failure becomes a warning in the output, never a changed verdict). This
is OBSERVABILITY, never a trading gate, never activation authority, and it never claims trading
performance.

Definitions (deterministic):
  * Explicit counts map 1:1 to health labels: structured_draft_count=healthy,
    fallback_count=fallback, missing_count=missing, invalid_count=invalid, empty_count=empty.
  * adoption_rate = healthy / (healthy + fallback) — the rate among sessions where candidates
    COULD have come from either path. `empty` (no draft AND no extractable report, e.g. a
    blackout No-Trade) carries no adoption signal and is excluded from the denominator;
    missing/invalid are pipeline failures, tracked separately. None when the denominator is 0.
  * consecutive_non_healthy_count: walking latest -> oldest, count fallback/missing/invalid
    until the first healthy; `empty` sessions are SKIPPED (they neither extend nor break the
    streak — a no-trade blackout day says nothing about adoption).
  * A malformed/unreadable artifact, a filename/embedded-target mismatch, or an unknown
    health_label is counted as INVALID (fail closed — never silently ignored).

Recommendation ladder (fail-closed order):
  investigate  any invalid; missing_count >= 2; fallback_count >= 3;
               consecutive_non_healthy_count >= 3; or an empty window (no data is never
               silent success).
  monitor      any fallback or a single missing in the window; or latest not healthy.
  healthy      latest is healthy AND zero invalid/missing AND zero fallback in the window.

ok = sessions_checked > 0 AND invalid_count == 0 AND missing_count == 0 (fallback alone keeps
ok=true — the pipeline worked, adoption just fell back).

CLI:
  candidate_artifact_health_rollup.py [--limit N] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                                      [--health-dir DIR] [--out PATH]
Exit 0 when ok=true, 1 when ok=false, 2 on usage/unexpected error. Std-lib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import tzutil  # noqa: E402

DEFAULT_HEALTH_DIR = ROOT / "data" / "runtime" / "health"
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_candidate_artifact_health\.json$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KNOWN_LABELS = ("healthy", "fallback", "empty", "missing", "invalid")
FAILURE_LABELS = ("fallback", "missing", "invalid")   # `empty` is NOT a failure

ROLLUP_HINTS = {
    "healthy": "none — structured candidate path is consistently active",
    "monitor": "adoption gap or a single gap-day in the window — watch the next runs and the "
               "per-run action hints",
    "investigate": "repeated fallback, any invalid artifact, repeated missing, or no data — "
                   "inspect data/runtime/health/*_candidate_artifact_health.json and the "
                   "freeze receipts before trusting candidate diagnostics",
}


def _load_session(path: Path, target: str) -> dict:
    """One per-run artifact -> {target, label, action_hint}. Anything structurally wrong is
    fail-closed to label=invalid (never silently ignored)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"target": target, "label": "invalid",
                "action_hint": f"unreadable health artifact: {path.name}"}
    if not isinstance(doc, dict):
        return {"target": target, "label": "invalid",
                "action_hint": f"non-object health artifact: {path.name}"}
    if str(doc.get("target_session_date")) != target:
        return {"target": target, "label": "invalid",
                "action_hint": f"filename/embedded target mismatch: {path.name}"}
    label = doc.get("health_label")
    if label not in KNOWN_LABELS:
        return {"target": target, "label": "invalid",
                "action_hint": f"unknown health_label {label!r}: {path.name}"}
    return {"target": target, "label": label,
            "action_hint": str(doc.get("action_hint") or "")[:300]}


def rollup(health_dir: Path | None = None, limit: int | None = None,
           date_from: str | None = None, date_to: str | None = None) -> dict:
    hdir = health_dir or DEFAULT_HEALTH_DIR
    sessions = []
    if hdir.is_dir():
        for p in sorted(hdir.iterdir()):
            m = FILE_RE.match(p.name)
            if not m:
                continue
            target = m.group(1)
            if date_from and target < date_from:
                continue
            if date_to and target > date_to:
                continue
            sessions.append(_load_session(p, target))
    sessions.sort(key=lambda s: s["target"])          # oldest -> newest
    if limit is not None and limit >= 0:
        sessions = sessions[-limit:]                  # most recent N

    counts = {label: 0 for label in KNOWN_LABELS}
    hints: list[str] = []
    for s in sessions:
        counts[s["label"]] += 1
        if s["label"] != "healthy" and s["action_hint"] and s["action_hint"] not in hints:
            hints.append(f"{s['target']}: {s['action_hint']}")

    n = len(sessions)
    healthy = counts["healthy"]
    fallback = counts["fallback"]
    missing = counts["missing"]
    invalid = counts["invalid"]
    empty = counts["empty"]
    denom = healthy + fallback
    adoption_rate = round(healthy / denom, 4) if denom else None

    consecutive = 0
    for s in reversed(sessions):                      # latest -> oldest
        if s["label"] == "healthy":
            break
        if s["label"] == "empty":
            continue
        consecutive += 1

    latest = sessions[-1] if sessions else None

    if n == 0 or invalid > 0 or missing >= 2 or fallback >= 3 or consecutive >= 3:
        recommendation = "investigate"
    elif fallback >= 1 or missing == 1 or (latest and latest["label"] != "healthy"):
        recommendation = "monitor"
    else:
        recommendation = "healthy"
    if recommendation == "healthy" and (latest is None or latest["label"] != "healthy"):
        recommendation = "monitor"                    # spec: healthy REQUIRES latest healthy

    return {
        "ok": n > 0 and invalid == 0 and missing == 0,
        "generated_utc": tzutil.stamps()["utc"],
        "window_start": sessions[0]["target"] if sessions else None,
        "window_end": sessions[-1]["target"] if sessions else None,
        "sessions_checked": n,
        "counts_by_health_label": counts,
        "structured_draft_count": healthy,
        "fallback_count": fallback,
        "missing_count": missing,
        "invalid_count": invalid,
        "empty_count": empty,
        "adoption_rate": adoption_rate,
        "latest_target": latest["target"] if latest else None,
        "latest_health_label": latest["label"] if latest else None,
        "consecutive_non_healthy_count": consecutive,
        "action_hints": ([ROLLUP_HINTS[recommendation]] + hints[:10]),
        "recommendation_label": recommendation,
        # Hard framing: adoption observability only; never performance, never authority.
        "diagnostic_only": True,
        "execution_enabled": False,
        "activation_authority": False,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Structured-candidate adoption rollup "
                                             "(read-only observability).")
    ap.add_argument("--limit", type=int, help="Most recent N sessions (after date filters).")
    ap.add_argument("--from", dest="date_from", help="Inclusive window start YYYY-MM-DD.")
    ap.add_argument("--to", dest="date_to", help="Inclusive window end YYYY-MM-DD.")
    ap.add_argument("--health-dir", help="Health dir override (tests).")
    ap.add_argument("--out", help="Also write the rollup JSON to this path (atomic, "
                                  "best-effort — a write failure becomes a warning).")
    args = ap.parse_args(argv)
    try:
        for name, v in (("--from", args.date_from), ("--to", args.date_to)):
            if v and not DATE_RE.match(v):
                print(json.dumps({"ok": False, "error": f"bad_date:{name}={v}",
                                  "diagnostic_only": True, "execution_enabled": False,
                                  "activation_authority": False}))
                return 2
        if args.date_from and args.date_to and args.date_from > args.date_to:
            print(json.dumps({"ok": False, "error": "from_after_to",
                              "diagnostic_only": True, "execution_enabled": False,
                              "activation_authority": False}))
            return 2
        out = rollup(health_dir=Path(args.health_dir) if args.health_dir else None,
                     limit=args.limit, date_from=args.date_from, date_to=args.date_to)
        if args.out:
            tmp = None
            try:
                dest = Path(args.out)
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
                os.replace(tmp, dest)
                tmp = None
            except Exception as exc:
                out["action_hints"].append(f"rollup_artifact_write_failed:{type(exc).__name__}")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["ok"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "diagnostic_only": True,
                          "execution_enabled": False, "activation_authority": False},
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
