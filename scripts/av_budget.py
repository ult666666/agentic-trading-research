#!/usr/bin/env python3
"""Alpha Vantage daily call-budget + deterministic file cache (Phase 4 / 4.1).

Scripts can't call MCP, so the SESSION uses this around each AV call:
  1) `cache-get <sig> --category <c>` — serve unchanged info (category TTL); no call, no budget.
  2) on miss: `reserve` — checks/decrements daily budget (fail-closed when exhausted).
  3) after the MCP call: `cache-put <sig> <jsonfile> --category <c> [--session <s>]`.
Discovery uses cached/low-cost sources; reserve AV calls for FINALIST verification.

Reliability: atomic writes (os.replace), corrupt-file recovery, daily PT reset, rich cache
metadata, per-category TTL, request dedupe via stable signatures. Std-lib only.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import tzutil  # noqa: E402

STATE = ROOT / "data" / "av_budget.json"
CACHE = ROOT / "data" / "av_cache"
DEFAULT_LIMIT = 25
CACHE_SCHEMA = "1.0"
# Protected reserves: low-priority "discovery" calls may NOT consume these.
FINALIST_RESERVE = 5     # reserved for finalist verification
EMERGENCY_RESERVE = 2    # reserved for bounded emergency retries

# Category-specific TTLs (minutes). Stale cache is NEVER served as current.
TTL_MIN = {
    "quote": 1,            # intraday quote — essentially no reuse
    "premarket": 2,
    "news": 60,
    "sec_filings": 720,
    "earnings_calendar": 720,
    "fundamentals": 1440,
    "daily_ohlcv": 1440,   # EOD daily series
    "tradability": 1440,
    "holiday_calendar": 43200,  # 30 days
    "default": 1440,
}


def _today_pt() -> str:
    return tzutil.pt_now().date().isoformat()


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)        # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state() -> dict:
    try:
        s = json.loads(STATE.read_text())
        if not isinstance(s, dict):
            raise ValueError("not an object")
    except Exception:
        s = {}  # corrupt/missing -> recover with a fresh state
    if s.get("date") != _today_pt():            # daily reset (PT)
        s = {"date": _today_pt(), "daily_limit": s.get("daily_limit", DEFAULT_LIMIT), "calls_used": 0}
    s.setdefault("daily_limit", DEFAULT_LIMIT)
    s.setdefault("calls_used", 0)
    return _annotate_reserves(s)


def _annotate_reserves(s: dict) -> dict:
    """Add UNAMBIGUOUS derived budget fields (consistent across load_state, save_state, status).

    Allocation policy: 'discovery' may use only what is left after BOTH protected reserves;
    'finalist' tasks may use discovery availability + the protected finalist reserve, preserving
    the emergency reserve; 'emergency' may use anything remaining.

    Invariant (at/above full reserves): remaining_total = discovery_available
        + protected_finalist_reserve + protected_emergency_reserve.
    """
    limit = s.get("daily_limit", DEFAULT_LIMIT)
    used = s.get("calls_used", 0)
    remaining = max(0, limit - used)
    s["used_total"] = used
    s["remaining_total"] = remaining
    s["protected_finalist_reserve"] = FINALIST_RESERVE      # configured reserve (5)
    s["protected_emergency_reserve"] = EMERGENCY_RESERVE     # configured reserve (2)
    s["discovery_available"] = max(0, remaining - FINALIST_RESERVE - EMERGENCY_RESERVE)
    s["finalist_task_max_available"] = max(0, remaining - EMERGENCY_RESERVE)  # NOT the 5-call reserve
    s["emergency_task_max_available"] = remaining
    s["budget_date_pt"] = s.get("date")
    s["last_updated_utc"] = tzutil.stamps()["utc_iso"]
    s["remaining"] = remaining  # backward-compat alias for remaining_total
    # drop legacy/ambiguous fields
    for k in ("finalist_reserve_remaining", "finalist_reserve", "emergency_reserve"):
        s.pop(k, None)
    return s


def save_state(s: dict):
    _annotate_reserves(s)
    _atomic_write(STATE, json.dumps(s, indent=2))


def reserve_check(state: dict, n: int, priority: str) -> dict:
    """Priority-aware reserve. 'discovery' may not dip into finalist+emergency reserves;
    'finalist' may not dip into the emergency reserve; 'emergency' may use anything remaining."""
    remaining = max(0, state["daily_limit"] - state["calls_used"])
    floor = {"discovery": FINALIST_RESERVE + EMERGENCY_RESERVE,
             "finalist": EMERGENCY_RESERVE, "emergency": 0}.get(priority, EMERGENCY_RESERVE)
    if remaining - n < floor:
        return {"granted": False, "reason": f"would_breach_{priority}_floor({floor})", "remaining": remaining}
    return {"granted": True, "remaining_after": remaining - n}


def sig(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def cache_path(s: str) -> Path:
    return CACHE / f"{s}.json"


def ttl_for(category: str) -> int:
    return TTL_MIN.get(category, TTL_MIN["default"])


def cache_get(signature: str, category: str = "default", max_age_min: int | None = None) -> dict:
    p = cache_path(signature)
    if not p.exists():
        return {"hit": False, "reason": "miss"}
    try:
        entry = json.loads(p.read_text())
        stored = datetime.fromisoformat(entry["retrieval_utc"])
        age = (tzutil.now_utc() - stored).total_seconds() / 60
    except Exception:
        return {"hit": False, "reason": "corrupt_cache_recovered"}  # corrupt -> treat as miss
    ttl = max_age_min if max_age_min is not None else entry.get("ttl_min", ttl_for(category))
    if age > ttl:
        return {"hit": False, "reason": "stale", "age_min": round(age, 1), "ttl_min": ttl}
    return {"hit": True, "age_min": round(age, 1), "ttl_min": ttl,
            "value": entry["value"], "metadata": entry.get("metadata", {})}


def cache_put(signature: str, value, category: str = "default", source: str = "alpha_vantage",
              endpoint: str = "", request_key: str = "", session: str = "") -> dict:
    payload = json.dumps(value, sort_keys=True)
    entry = {
        "schema_version": CACHE_SCHEMA,
        "retrieval_utc": tzutil.stamps()["utc_iso"],
        "ttl_min": ttl_for(category),
        "expires_hint": "retrieval_utc + ttl_min",
        "value": value,
        "metadata": {"source": source, "endpoint": endpoint, "request_key": request_key or signature,
                     "category": category, "market_session": session,
                     "payload_sha256": hashlib.sha256(payload.encode()).hexdigest()[:16], "state": "success"},
    }
    _atomic_write(cache_path(signature), json.dumps(entry, indent=2))
    return {"cached": True, "signature": signature, "ttl_min": entry["ttl_min"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("reserve"); r.add_argument("-n", type=int, default=1)
    r.add_argument("--priority", choices=["discovery", "finalist", "emergency"], default="finalist")
    sg = sub.add_parser("sig"); sg.add_argument("parts", nargs="+")
    cg = sub.add_parser("cache-get"); cg.add_argument("signature"); cg.add_argument("--category", default="default")
    cg.add_argument("--max-age-min", type=int, default=None)
    cp = sub.add_parser("cache-put"); cp.add_argument("signature"); cp.add_argument("jsonfile")
    cp.add_argument("--category", default="default"); cp.add_argument("--endpoint", default="")
    cp.add_argument("--session", default="")
    sl = sub.add_parser("set-limit"); sl.add_argument("limit", type=int)
    a = ap.parse_args()

    if a.cmd == "status":
        print(json.dumps(load_state(), indent=2)); return 0
    if a.cmd == "set-limit":
        s = load_state(); s["daily_limit"] = a.limit; save_state(s); print(json.dumps(s, indent=2)); return 0
    if a.cmd == "sig":
        print(sig(*a.parts)); return 0
    if a.cmd == "reserve":
        s = load_state()
        rc = reserve_check(s, a.n, a.priority)
        if not rc["granted"]:
            print(json.dumps({**rc, "priority": a.priority, **s})); return 1
        s["calls_used"] += a.n; save_state(s)
        print(json.dumps({"granted": True, "priority": a.priority, **s})); return 0
    if a.cmd == "cache-get":
        r = cache_get(a.signature, a.category, a.max_age_min)
        print(json.dumps(r)); return 0 if r["hit"] else 1
    if a.cmd == "cache-put":
        value = json.loads(Path(a.jsonfile).read_text())
        print(json.dumps(cache_put(a.signature, value, a.category, endpoint=a.endpoint, session=a.session))); return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
