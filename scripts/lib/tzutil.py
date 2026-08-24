#!/usr/bin/env python3
"""Shared timezone utility — the ONLY sanctioned way to handle time in this project.

Rules (see CLAUDE.md): operating TZ = America/Los_Angeles; US market TZ = America/New_York;
logs/machine timestamps = UTC. NEVER convert with assumed fixed offsets — always use these
zoneinfo-backed helpers so DST is handled automatically.

All scripts, hooks, and freeze logic import from here. Std-lib only (zoneinfo, datetime).
"""
from __future__ import annotations
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")   # operating timezone
ET = ZoneInfo("America/New_York")      # US market timezone
UTC = ZoneInfo("UTC")                  # logs / machine timestamps

# Approved morning-freeze window (PT). An Official Morning Report may only freeze in
# [EARLIEST_FREEZE, LATEST_FREEZE] and must be before the regular-market open.
RESEARCH_START = time(5, 0)    # 05:00 PT — begin morning research
EARLIEST_FREEZE = time(5, 45)  # 05:45 PT — earliest valid official freeze
TARGET_FREEZE = time(6, 5)     # 06:05 PT — target freeze
LATEST_FREEZE = time(6, 20)    # 06:20 PT — latest normal freeze
MARKET_OPEN_PT = time(6, 30)   # 06:30 PT — regular session open (must publish before this)


def now_utc() -> datetime:
    """Timezone-aware current time in UTC. Single source of 'now'."""
    return datetime.now(UTC)


def to_pt(dt: datetime) -> datetime:
    return _aware(dt).astimezone(PT)


def to_et(dt: datetime) -> datetime:
    return _aware(dt).astimezone(ET)


def to_utc(dt: datetime) -> datetime:
    return _aware(dt).astimezone(UTC)


def _aware(dt: datetime) -> datetime:
    """Reject naive datetimes — never assume a zone."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to tzutil; supply a tz-aware datetime")
    return dt


def pt_now() -> datetime:
    return to_pt(now_utc())


def stamps(dt: datetime | None = None) -> dict:
    """Return the canonical triple-stamp (+ target session date) for a moment.

    The 'target_session_date' is the US trading session a report generated at `dt` is FOR:
    the ET calendar date if generated at/after the 09:30 ET open is that day; otherwise — the
    common case for evening/overnight/pre-open generation — it is the *next* US calendar day
    handled by the caller. Here we expose the ET date plus a 'next_et_date' convenience.
    """
    dt = dt or now_utc()
    p, e, u = to_pt(dt), to_et(dt), to_utc(dt)
    return {
        "pt": p.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "et": e.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "utc": u.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "pt_iso": p.isoformat(),
        "et_iso": e.isoformat(),
        "utc_iso": u.isoformat(),
        "et_date": e.date().isoformat(),
        "next_et_date": (e.date() + timedelta(days=1)).isoformat(),
    }


def triple_line(dt: datetime | None = None) -> str:
    """One-line human stamp for report headers."""
    s = stamps(dt)
    return f"PT {s['pt']} | ET {s['et']} | UTC {s['utc']}"


def in_morning_freeze_window(dt: datetime | None = None) -> bool:
    """True iff the PT wall-clock time is within [EARLIEST_FREEZE, LATEST_FREEZE]."""
    t = to_pt(dt or now_utc()).time()
    return EARLIEST_FREEZE <= t <= LATEST_FREEZE


def before_market_open(dt: datetime | None = None) -> bool:
    """DEPRECATED for cross-date use — time-of-day only. Prefer is_after_session_open()."""
    return to_pt(dt or now_utc()).time() < MARKET_OPEN_PT


def session_open_dt(target_session_date: str) -> datetime:
    """Timezone-aware 06:30 America/Los_Angeles regular-market open for the given session date.

    Built from the PT wall-clock open on that date, so DST is resolved automatically
    (PDT in summer, PST in winter) by zoneinfo — never via assumed offsets.
    """
    d = date.fromisoformat(target_session_date)
    return datetime(d.year, d.month, d.day, MARKET_OPEN_PT.hour, MARKET_OPEN_PT.minute, tzinfo=PT)


def is_after_session_open(dt: datetime, target_session_date: str) -> bool:
    """True iff `dt` is strictly later than the target session's actual market open.

    Compares full timezone-aware instants (in UTC), NOT time-of-day. So the evening before a
    session (e.g. 2026-06-22 21:20 PT vs the 2026-06-23 open) is correctly 'before open'.
    """
    return to_utc(_aware(dt)) > to_utc(session_open_dt(target_session_date))


def suggest_preliminary_stage(target_session_date: str, dt: datetime | None = None) -> str:
    """When an Official Morning Report is NOT allowed, pick the correct preliminary stage.

    - 'evening_preliminary'  : generated on a calendar date BEFORE the target session (PT).
    - 'overnight_preliminary': generated on the target session's PT date but before the window.
    - 'post_window_preliminary': generated on the session date but after the latest freeze
      (too late to be official) — still not an Official Morning Report.
    """
    p = to_pt(dt or now_utc())
    tgt = date.fromisoformat(target_session_date)
    if p.date() < tgt:
        return "evening_preliminary"
    if p.date() == tgt:
        return "overnight_preliminary" if p.time() < EARLIEST_FREEZE else "post_window_preliminary"
    return "stale_preliminary"  # generated AFTER the target session date — cannot be official


if __name__ == "__main__":
    import json, sys
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    dt = datetime.fromisoformat(arg).astimezone(UTC) if arg else now_utc()
    print(json.dumps(stamps(dt), indent=2))
