#!/usr/bin/env python3
"""Shared audited-window marker — the source-side fence primitive (reference implementation).

WHY THIS EXISTS
---------------
The control-plane integrity guard (scripts/control_plane_integrity.py + scripts/audited_claude.py)
is authorship-agnostic and fail-closed by design: it snapshots the control plane before an audited
child, and quarantines ANY byte change after. That is correct. The recurring pain is TIMING, not
the guard:

  * 2026-07-20 morning rc=88 — the 05:46 watchdog wrote data/system_state.json inside the window.
  * 2026-07-21 post_close rc=88 — a concurrent development session wrote a new config/ file (via a
    sanctioned writer; the model file-tool hooks already block DIRECT model writes to config/)
    inside the 13:15-13:50 audited window, so a completed 35-minute diagnostic run was quarantined.

The model hooks already prevent DIRECT model writes to the control plane. The remaining gap is a
*sanctioned* writer — an operator applier, a manifest re-sign, or a deterministic script — mutating
a control-plane path while an audited window is open. This primitive lets those writers SEE that a
window is open and refuse, so the collision never reaches the guard.

DESIGN CONTRACT
---------------
1. The marker lives at data/runtime/audited_window/<invocation_id>.json — deliberately OUTSIDE
   every CONTROL_TREE and CONTROL_FILE and with NO target-date substring, so writing/removing the
   marker can never itself trip the control-plane comparison or the authority-surface sweep.
2. FAIL-OPEN on every read/parse error. A guard that fail-CLOSED here would let a crashed audited
   run deadlock every future operator action. The control-plane comparison remains the backstop.
3. Liveness is by EXPIRY, not pid — pids are reused and cross-process liveness is unreliable. A
   marker past its expiry is ignored. TTL exceeds the 3600s max supervisor bound with margin.
4. Read/write only; grants no authority, invokes no broker, stdlib-only, no network.
   is_window_active() is pure-read.

Reference implementation for adoption into scripts/lib/ under the sentry Step-0 authority gate;
see DESIGN.md for exact wiring points. Unit-tested by test_audited_window.py in this directory.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "audited_window_marker/1.0"
MARKER_DIRNAME = ("data", "runtime", "audited_window")
DEFAULT_TTL_SECONDS = 4200  # strictly > the 3600s max supervisor bound (post_close), with margin


def _marker_dir(root: Path) -> Path:
    return Path(root).resolve().joinpath(*MARKER_DIRNAME)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def open_window(root: Path, *, lane: str, run_id: str, invocation_id: str,
                now_utc: datetime, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Path:
    """Create the marker for an audited window. Call after the control-plane snapshot is taken.

    Returns the marker path (pass it to close_window in a finally block). Writing the marker is
    atomic (temp + os.replace) so a concurrent reader never sees a half-written file.
    """
    directory = _marker_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    now_utc = now_utc.astimezone(timezone.utc)
    expiry = now_utc.timestamp() + max(1, int(ttl_seconds))
    document = {
        "schema": SCHEMA,
        "lane": str(lane),
        "run_id": str(run_id),
        "invocation_id": str(invocation_id),
        "pid": os.getpid(),
        "opened_utc": now_utc.isoformat(),
        "expiry_utc": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
    }
    path = directory / f"{invocation_id}.json"
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".mk-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def close_window(path: Path | None) -> None:
    """Best-effort removal of a marker. Safe to call more than once and on None."""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def _live_markers(root: Path, now_utc: datetime) -> list[dict[str, Any]]:
    directory = _marker_dir(root)
    now_utc = now_utc.astimezone(timezone.utc)
    live: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return live  # FAIL-OPEN: unreadable marker dir means "no active window".
    for entry in entries:
        try:
            document = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # malformed / vanished marker is ignored, never fatal
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            continue
        expiry = _parse_utc(document.get("expiry_utc"))
        if expiry is None or now_utc >= expiry:
            continue  # missing or passed expiry -> stale, ignored (fail-open)
        live.append({
            "lane": document.get("lane"),
            "run_id": document.get("run_id"),
            "invocation_id": document.get("invocation_id"),
            "opened_utc": document.get("opened_utc"),
            "expiry_utc": document.get("expiry_utc"),
            "marker_path": str(entry),
        })
    return live


def is_window_active(root: Path, now_utc: datetime) -> tuple[bool, list[dict[str, Any]]]:
    """Pure read. Returns (active, [live markers]). Fail-open: any read error -> (False, [])."""
    markers = _live_markers(root, now_utc)
    return (bool(markers), markers)


def describe_active(root: Path, now_utc: datetime) -> str:
    """Human-facing one-liner naming the open windows and their expiries (for refusal messages)."""
    active, markers = is_window_active(root, now_utc)
    if not active:
        return "no audited window is active"
    parts = [f"{m.get('lane')} ({m.get('invocation_id')}) until {m.get('expiry_utc')}"
             for m in markers]
    return "audited window(s) active: " + "; ".join(parts)
