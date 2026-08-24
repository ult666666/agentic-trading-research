#!/usr/bin/env python3
"""Consecutive-session persistence for autonomy service-SLO misses.

WHY
---
On 2026-07-29..07-31 every scheduled workflow missed its SLO on three consecutive trading
sessions. The alert layer fingerprints the ACTIVE CONDITION SET, and that set was byte-identical
each morning, so every session produced `transition: "unchanged"` and therefore
`notification_required: false`. A total automation outage decayed into silence.

The missing concept is DURATION. A condition that has persisted across consecutive expected
trading sessions is not the same condition as one that appeared this morning, and it must not
share a fingerprint. This module supplies that duration from durable evidence so the alert layer
can escalate WITHOUT touching the `repeat_unchanged_alerts: false` anti-spam contract.

CONTRACT
--------
* Read-only. Reads composite health receipts and the market calendar; writes nothing, invokes no
  broker, model, or network, and grants no authority. `execution_enabled` is irrelevant here and
  never becomes true.
* The streak is DERIVED from `data/runtime/health/<date>_autonomy_service_health.json`, never
  from a mutable counter, so it survives process restarts, state resets, and replays.
* Expected sessions come from the trading calendar, never from calendar days: weekends and
  exchange holidays cannot advance a streak.
* `not_applicable` (the calendar's own "not required this session" verdict) and `pending` are
  NEUTRAL: they neither advance nor reset a streak.
* Malformed, contradictory, or mis-targeted history FAILS CLOSED with a specific diagnostic
  instead of silently resetting the outage to zero.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import validate_market_day

SCHEMA = "slo_persistence/1.0"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The status vocabulary produced by autonomy_service_health._service_slo_row.
MISS_STATUS = "missed"
RESET_STATUSES = frozenset({"met"})
# Neutral: a session that was never required, or is not finished yet, is not evidence either way.
# A future "workflow disabled" / "maintenance hold" primitive belongs in this set, not beside it.
NEUTRAL_STATUSES = frozenset({"not_applicable", "pending"})
INVALID_STATUSES = frozenset({"invalid"})

# Two consecutive missed expected sessions is the escalation threshold: one missed morning is a
# bad day, two in a row is a broken system. Deliberately small — the 2026-07 outage ran for three.
PERSISTENT_THRESHOLD = 2
# Bounded walk: enough to span a long weekend plus a holiday and still see a working session.
MAX_LOOKBACK_SESSIONS = 12

HEALTH_DIR = ("data", "runtime", "health")


def _health_path(root: Path, date: str) -> Path:
    return Path(root).joinpath(*HEALTH_DIR) / f"{date}_autonomy_service_health.json"


def _parse_date(value: str) -> dt.date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("date_invalid")
    return dt.date.fromisoformat(value)


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo is not None else None


def prior_trading_sessions(target: str, count: int, *,
                           calendar: tuple | None = None) -> list[str]:
    """The `count` trading sessions immediately BEFORE `target`, most recent first.

    Uses the repository's own exchange calendar, so weekends, exchange holidays, and any other
    non-trading day are skipped rather than counted. A year the calendar does not cover stops
    the walk: an uncovered year is missing evidence, not a run of clean sessions.
    """
    holidays, early = calendar if calendar is not None else validate_market_day.load_calendar(
        validate_market_day.CONFIG)
    sessions: list[str] = []
    cursor = _parse_date(target)
    # Bounded by construction: at most ~2 calendar weeks per requested session.
    for _ in range(count * 10 + 14):
        if len(sessions) >= count:
            break
        cursor -= dt.timedelta(days=1)
        verdict = validate_market_day.verdict(cursor, holidays, early)
        if not isinstance(verdict, dict) or verdict.get("calendar_covers_year") is not True:
            break
        if verdict.get("is_trading_day") is True:
            sessions.append(cursor.isoformat())
    return sessions


def session_status(root: Path, date: str, workflow: str) -> tuple[str | None, str | None]:
    """(status, diagnostic) for one workflow on one past session.

    A diagnostic is returned for evidence that EXISTS but cannot be trusted. A receipt that is
    simply absent returns (None, None): unknown is not the same as broken.
    """
    path = _health_path(root, date)
    if not path.is_file():
        return None, None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"history_unreadable:{date}"
    if not isinstance(document, dict):
        return None, f"history_not_object:{date}"
    if document.get("target_session_date") != date:
        # A receipt filed under the wrong session cannot be attributed to either one.
        return None, f"history_target_mismatch:{date}"
    service = document.get("service_slo_health")
    if not isinstance(service, dict):
        return None, f"history_service_plane_invalid:{date}"
    row = service.get(workflow)
    if row is None:
        return None, None
    if not isinstance(row, dict):
        return None, f"history_workflow_row_invalid:{date}"
    status = row.get("status")
    if not isinstance(status, str) or not status:
        return None, f"history_status_invalid:{date}"
    return status, None


def _generated_utc(root: Path, date: str) -> dt.datetime | None:
    path = _health_path(root, date)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse_utc(document.get("generated_utc")) if isinstance(document, dict) else None


def consecutive_missed(*, workflow: str, target: str, current_status: str,
                       root: Path, calendar: tuple | None = None,
                       max_lookback: int = MAX_LOOKBACK_SESSIONS) -> dict:
    """How many consecutive EXPECTED sessions ending at `target` this workflow has missed.

    `target` itself is included: a first miss returns streak 1, so the caller escalates on
    `streak >= PERSISTENT_THRESHOLD` without an off-by-one.
    """
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "workflow": workflow,
        "streak": 0,
        "sessions": [],
        "diagnostics": [],
        "history_complete": True,
        # `history_complete` false merely means the walk stopped early (often just an absent
        # older receipt). `history_conflict` is the stronger claim: evidence EXISTS and is
        # self-contradictory, which is an integrity fault an operator must see.
        "history_conflict": False,
    }
    if current_status != MISS_STATUS:
        return result

    result["streak"] = 1
    result["sessions"] = [target]
    diagnostics: list[str] = []
    try:
        prior = prior_trading_sessions(target, max_lookback, calendar=calendar)
    except ValueError:
        result["diagnostics"] = ["history_target_date_invalid"]
        result["history_complete"] = False
        result["history_conflict"] = True
        return result

    if not prior:
        result["history_complete"] = False
        diagnostics.append("history_calendar_unavailable")

    target_generated = _generated_utc(root, target)
    for date in prior:
        status, diagnostic = session_status(root, date, workflow)
        if diagnostic is not None:
            # Contradictory evidence must not silently reset an outage to zero.
            diagnostics.append(diagnostic)
            result["history_complete"] = False
            result["history_conflict"] = True
            break
        if status is None:
            # No receipt for an expected session: the streak cannot be PROVEN further back.
            result["history_complete"] = False
            diagnostics.append(f"history_gap:{date}")
            break
        generated = _generated_utc(root, date)
        if (generated is not None and target_generated is not None
                and generated > target_generated):
            diagnostics.append(f"history_out_of_order:{date}")
            result["history_complete"] = False
            result["history_conflict"] = True
            break
        if status in NEUTRAL_STATUSES:
            continue
        if status in INVALID_STATUSES:
            diagnostics.append(f"history_session_invalid:{date}")
            result["history_complete"] = False
            result["history_conflict"] = True
            break
        if status in RESET_STATUSES:
            break
        if status == MISS_STATUS:
            result["streak"] += 1
            result["sessions"].append(date)
            continue
        diagnostics.append(f"history_status_unknown:{date}")
        result["history_complete"] = False
        result["history_conflict"] = True
        break

    result["diagnostics"] = sorted(set(diagnostics))
    return result


def is_persistent(streak: int) -> bool:
    return isinstance(streak, int) and streak >= PERSISTENT_THRESHOLD
