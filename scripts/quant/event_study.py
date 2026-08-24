#!/usr/bin/env python3
"""Offline quant lab — EVENT STUDY (Layer 3 skeleton). DIAGNOSTIC ONLY.

Safety contract:
  * Research lab output. Never feeds production scoring/gates; every result (including
    refusals) self-labels with the decision_path diagnostic-exclusion fields via
    quant.DIAGNOSTIC_FIELDS so it can never be counted as strategy performance.
  * Fail-closed: unknown symbol, event date that is not a bar date, malformed bars, or
    insufficient surrounding history => {"error": ...} + exit 1. No partial results.
  * Strict point-in-time (mirrors scripts/replay_session.py discipline): ATR(14) uses only
    bars strictly BEFORE the event date; window math touches only bars inside
    [event + min_window_start - 1, event + max_window_end]. Results are identical when all
    bars beyond the widest requested window are deleted (no look-ahead).
  * NEVER fabricate: data/replay_bars.json carries no volume, so volume_shock is emitted
    as null with reason "volume_unavailable_in_archive".

Window return convention: for window [a, b] in TRADING-day offsets from the event date t,
raw return = close[t+b] / close[t+a-1] - 1 (cumulative close-to-close return earned over
days a..b inclusive). The SPY-adjusted abnormal return subtracts the benchmark's return
over the SAME calendar dates. ATR-normalized move = (close[t+b] - close[t+a-1]) / ATR14,
with ATR14 computed from the 14 sessions ending at t-1. Gap = open[t]/close[t-1] - 1.

Run: python3 scripts/quant/event_study.py --symbol AAPL --event-date 2026-05-18
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import DIAGNOSTIC_FIELDS  # noqa: E402

DEFAULT_BARS = ROOT / "data" / "replay_bars.json"
DEFAULT_WINDOWS = ((-10, 10), (-3, 3), (0, 1), (1, 5))
ATR_LEN = 14                       # sessions, strictly before the event date
DEFAULT_BENCHMARK = "SPY"
_NUM = (int, float)


def _bars_map(archive):
    """Accept the replay archive shape ({"bars": {SYM: [rows]}}) or a plain {SYM: [rows]}."""
    if not isinstance(archive, dict):
        return None
    inner = archive.get("bars", archive)
    if not isinstance(inner, dict) or not inner:
        return None
    return inner


def _valid_bar(row) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("date"), str):
        return False
    return all(isinstance(row.get(k), _NUM) and not isinstance(row.get(k), bool)
               and row.get(k) > 0 for k in ("open", "high", "low", "close"))


def _atr14(rows, idx):
    """ATR over the ATR_LEN sessions ending at idx-1 — strictly before the event (point-in-time).
    Requires a FULL lookback (idx >= ATR_LEN + 1); a partial ATR would be a silent degradation."""
    if idx < ATR_LEN + 1:
        return None
    trs = []
    for i in range(idx - ATR_LEN, idx):
        hi, lo, pc = rows[i]["high"], rows[i]["low"], rows[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / len(trs)


def event_study(archive, symbol, event_date, windows=DEFAULT_WINDOWS,
                benchmark=DEFAULT_BENCHMARK):
    """Compute per-window event-study diagnostics. Returns a dict; refusals carry "error"."""
    base = dict(DIAGNOSTIC_FIELDS)
    base.update({"tool": "quant.event_study", "symbol": symbol, "event_date": event_date,
                 "benchmark": benchmark})
    if isinstance(archive, dict) and isinstance(archive.get("source"), str):
        base["data_provenance"] = archive["source"]

    def refuse(err, **extra):
        return {**base, "error": err, **extra}

    try:
        windows = [(int(a), int(b)) for a, b in windows]
    except (TypeError, ValueError):
        return refuse("invalid_windows")
    if not windows or any(a > b for a, b in windows):
        return refuse("invalid_windows")

    bars = _bars_map(archive)
    if bars is None:
        return refuse("malformed_bars_container")
    if symbol not in bars:
        return refuse("symbol_not_in_archive", available_symbols=sorted(bars))
    if benchmark not in bars:
        return refuse("benchmark_not_in_archive", available_symbols=sorted(bars))

    rows = bars[symbol]
    if not isinstance(rows, list) or not rows:
        return refuse("no_bars_for_symbol")
    dates = [r.get("date") if isinstance(r, dict) else None for r in rows]
    if event_date not in dates:
        return refuse("event_date_not_a_bar_date",
                      archive_first=dates[0], archive_last=dates[-1])
    idx = dates.index(event_date)

    min_a = min(a for a, _ in windows)
    max_b = max(b for _, b in windows)
    # Point-in-time requirements: window start close lives at t+a-1; ATR needs a full
    # ATR_LEN lookback plus one prior close (t - ATR_LEN - 1 .. t - 1).
    need_before = max(ATR_LEN + 1, 1 - min_a)
    need_after = max(0, max_b)
    if idx < need_before:
        return refuse("insufficient_bars_before_event",
                      required_bars_before=need_before, available_bars_before=idx)
    if idx + need_after > len(rows) - 1:
        return refuse("insufficient_bars_after_event",
                      required_bars_after=need_after,
                      available_bars_after=len(rows) - 1 - idx)

    # Validate ONLY the bars this study is allowed to touch (no peeking beyond the window).
    for i in range(idx - need_before, idx + need_after + 1):
        if not _valid_bar(rows[i]):
            return refuse("malformed_bar", symbol=symbol, index=i)

    bench_rows = bars[benchmark]
    if not isinstance(bench_rows, list):
        return refuse("no_bars_for_benchmark")
    bench_by_date = {r["date"]: r for r in bench_rows if _valid_bar(r)}

    atr = _atr14(rows, idx)
    if atr is None or atr <= 0:
        return refuse("atr_unavailable_point_in_time", atr_len=ATR_LEN)
    gap = rows[idx]["open"] / rows[idx - 1]["close"] - 1

    win_out = []
    for a, b in windows:
        i0, i1 = idx + a - 1, idx + b
        d0, d1 = rows[i0]["date"], rows[i1]["date"]
        c0, c1 = rows[i0]["close"], rows[i1]["close"]
        if d0 not in bench_by_date or d1 not in bench_by_date:
            return refuse("benchmark_missing_window_dates", window=[a, b],
                          needed_dates=[d0, d1])
        raw = c1 / c0 - 1
        braw = bench_by_date[d1]["close"] / bench_by_date[d0]["close"] - 1
        # Volume shock: the archive has no volume — emit null + reason, NEVER fabricate.
        has_vol = all(isinstance(rows[i].get("volume"), _NUM)
                      and not isinstance(rows[i].get("volume"), bool)
                      for i in range(idx - need_before, idx + need_after + 1))
        win_out.append({
            "window": [a, b],
            "window_dates": {"return_base_close": d0, "return_end_close": d1},
            "raw_return": round(raw, 8),
            "abnormal_return_vs_benchmark": round(raw - braw, 8),
            "benchmark_return": round(braw, 8),
            "atr_normalized_move": round((c1 - c0) / atr, 8),
            "gap_at_event_open": round(gap, 8),
            "volume_shock": None,
            "volume_shock_reason": (None if has_vol else "volume_unavailable_in_archive"),
        })
        # Skeleton: even if volume were present we refuse to compute a shock without an
        # approved definition — a wrong number is worse than a labeled null.
        if has_vol:
            win_out[-1]["volume_shock_reason"] = "volume_shock_definition_not_yet_approved"

    return {**base, "atr14_pre_event": round(atr, 8), "gap_at_event_open": round(gap, 8),
            "windows_convention": "raw = close[t+b]/close[t+a-1]-1 over trading days a..b",
            "windows": win_out}


def _parse_windows(spec):
    out = []
    for part in spec.split(","):
        a, b = part.split(":")
        out.append((int(a), int(b)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Diagnostic event study over replay bars (research lab only).")
    ap.add_argument("--bars", default=str(DEFAULT_BARS), help="path to replay-bars JSON archive")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--event-date", required=True, help="YYYY-MM-DD; must be a bar date")
    ap.add_argument("--windows", default="-10:10,-3:3,0:1,1:5",
                    help='comma-separated a:b trading-day windows, e.g. "-10:10,0:1"')
    ap.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    args = ap.parse_args(argv)

    def fail(err, **extra):
        print(json.dumps({**DIAGNOSTIC_FIELDS, "tool": "quant.event_study",
                          "error": err, **extra}, indent=2))
        return 1

    try:
        windows = _parse_windows(args.windows)
    except ValueError:
        return fail("invalid_windows_spec", windows_spec=args.windows)
    path = Path(args.bars)
    if not path.is_file():
        return fail("bars_file_missing", bars_path=str(path))
    try:
        archive = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return fail("bars_file_malformed", bars_path=str(path))

    result = event_study(archive, args.symbol, args.event_date,
                         windows=windows, benchmark=args.benchmark)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
