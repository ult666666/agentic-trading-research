#!/usr/bin/env python3
"""Trusted derived system-state publisher (canonical workflow names, receipt-derived).

WHY (2026-07-28): `data/system_state.json` `runs.*` entries are written only by runner-level
`record_state()` under LEGACY task names (`morning_scan`, `premarket_refresh`,
`post_close_review`). Outcomes decided by the autonomy orchestrator BEFORE a child runner exists
(dependency-gate skips, manifest/authority/ownership blocks) never reach the file at all, so on
2026-07-28 it presented `premarket_refresh: dry_run_ok @ 2026-07-16` and
`post_close_review: failed @ 2026-07-24` as current state on a day when post_open had
definitively skipped pre-child. This module derives a CANONICAL `workflows` section from the
authoritative per-day autonomy receipt (`data/runtime/autonomy/<target>.json`) at every
orchestrator finalize — pre-child and post-child — so canonical workflow names always outrank
the legacy keys. The legacy `runs` section is preserved verbatim and is never rewritten here.

CONCURRENCY (2026-07-28 review D11): this is a read-modify-write on a file that `record_state()`
in scripts/lib/common.sh also read-modify-writes. Both sides now take an exclusive flock on a
sibling lock file, and a publish that cannot read valid existing bytes REFUSES to write rather
than replacing the document (which would destroy the legacy `runs` section).

FENCE BEHAVIOUR (2026-07-28 review D10 — stated precisely): while an audited model window is
open this publisher defers. If the fence primitive itself cannot be evaluated it proceeds,
because `data/system_state.json` is listed in
`control_plane_integrity.COMPARISON_EXEMPT_CONTROL_FILES` and is therefore excluded from the
audited control-plane comparison that produces rc=88. That exemption is the load-bearing safety
property, so it is asserted by test rather than assumed.

Safety: statuses/timestamps/run-ids only — never prices, balances, positions, credentials, or
model output. Best-effort by contract: callers swallow exceptions; observability must never fail
a run.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "system_state_workflows/1.0"
WORKFLOWS = ("intake", "macro_refresh", "morning", "post_open", "post_close")
LEGACY_TASK_KEYS = {
    "intake": "data_intake",
    "macro_refresh": "macro_actual_refresh",
    "morning": "morning_scan",
    "post_open": "premarket_refresh",
    "post_close": "post_close_review",
}
TARGET_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
EXEMPT_STATE_FILE = "data/system_state.json"


def _now_iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _safe(value: object) -> str | None:
    text = str(value) if value is not None else ""
    return text if TOKEN_RE.fullmatch(text) else None


def _window_active(root: Path) -> bool:
    """True when an audited model window is open. See FENCE BEHAVIOUR in the module docstring."""
    try:
        import sys
        lib = str(Path(root) / "scripts" / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import audited_window
        active, _detail = audited_window.is_window_active(
            Path(root), datetime.now(timezone.utc))
        return bool(active)
    except Exception:
        return False


def derive_workflows(state_document: dict | None, target: str) -> dict:
    entries: dict[str, dict] = {}
    workflows = (state_document or {}).get("workflows")
    workflows = workflows if isinstance(workflows, dict) else {}
    for name in WORKFLOWS:
        row = workflows.get(name)
        row = row if isinstance(row, dict) else {}
        attempts = row.get("attempts") if isinstance(row.get("attempts"), list) else []
        latest = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        entries[name] = {
            "state": _safe(row.get("state")) or "pending",
            "outcome": _safe(latest.get("outcome")),
            "run_id": _safe(row.get("latest_run_id")),
            "exit_code": (latest.get("exit_code")
                          if isinstance(latest.get("exit_code"), int) else None),
            "child_spawned": latest.get("child_pid") is not None,
            "started_utc": _safe(latest.get("started_utc")),
            "finished_utc": _safe(latest.get("finished_utc")),
            "target_session_date": target,
            "legacy_task_key": LEGACY_TASK_KEYS[name],
        }
    return entries


def publish(root: Path, target: str, *, state_path: Path | None = None,
            now: datetime | None = None) -> dict:
    root = Path(root)
    if not TARGET_RE.fullmatch(str(target)):
        return {"ok": False, "deferred": True, "reason": "target_invalid"}
    if _window_active(root):
        return {"ok": True, "deferred": True, "reason": "audited_window_active"}
    state_path = state_path or (root / "data" / "runtime" / "autonomy" / f"{target}.json")
    try:
        state_document = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state_document = None

    system_path = root / "data" / "system_state.json"
    system_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = system_path.with_name(system_path.name + ".publish.lock")
    stamp = _now_iso(now)
    with open(lock_path, "a+b") as lock:
        with contextlib.suppress(OSError):
            os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existed = system_path.exists()
        try:
            system_document = json.loads(system_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            system_document = {}
        except (OSError, ValueError):
            # NEVER replace an unreadable state document: doing so would destroy the legacy
            # runs.* section that record_state() owns (2026-07-28 review D11).
            return {"ok": False, "deferred": True, "reason": "existing_state_unreadable"}
        if not isinstance(system_document, dict):
            if existed:
                return {"ok": False, "deferred": True, "reason": "existing_state_not_object"}
            system_document = {}

        system_document["workflows"] = {
            "schema": SCHEMA,
            "target_session_date": target,
            "updated_utc": stamp,
            "source": "data/runtime/autonomy daily receipt (authoritative); legacy runs.* "
                      "entries are runner-written, may be stale, and never outrank this section",
            "entries": derive_workflows(state_document, target),
        }
        system_document["last_update_utc"] = stamp

        fd, temporary = tempfile.mkstemp(dir=str(system_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(system_document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, system_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    return {"ok": True, "deferred": False, "path": str(system_path),
            "entries": len(system_document["workflows"]["entries"])}
