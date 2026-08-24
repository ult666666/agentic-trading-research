#!/usr/bin/env python3
"""Preserve reports without overwriting.

Copies a source file (or stdin) into reports/<category>/<date>.md. If that path already
exists, appends -2, -3, ... so prior reports are never destroyed. Std-lib only.

OFFICIAL-SLOT PROTECTION (deterministic authority): new canonical morning publications may ONLY
be written by scripts/research_freeze.py after receipt/evidence/source gates pass; the historical
`reports/morning/<YYYY-MM-DD>.md` legacy slot is read-only. This archiver therefore NEVER writes the top-level
morning slot: category `morning` is routed to `reports/morning/preliminary/<date>_unfrozen_
<HHMM>PT.md` so an ungated archive can never masquerade as a frozen Official report. Other
categories (intraday/post_close/weekly) are not freeze-gated and archive as before.

Usage:
  python3 scripts/archive_report.py morning path/to/draft.md [--date 2026-06-22]
  echo "..." | python3 scripts/archive_report.py post_close - --date 2026-06-22
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = {"morning", "intraday", "post_close", "weekly"}

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:
    PT = None


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category", choices=sorted(CATEGORIES))
    ap.add_argument("source", help="source .md file, or - for stdin")
    ap.add_argument("--date", help="YYYY-MM-DD (or YYYY-Www for weekly); default today PT")
    ap.add_argument("--root", help="override reports root (tests only; default <project>/reports)")
    args = ap.parse_args()
    try:
        content = sys.stdin.read() if args.source == "-" else Path(args.source).read_text()
        now = datetime.now(PT) if PT else datetime.now()
        if args.date:
            stem = args.date
        elif args.category == "weekly":
            stem = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        else:
            stem = now.strftime("%Y-%m-%d")
            if args.category == "intraday":
                stem += "-postopen"
        out_dir = Path(args.root) if args.root else (ROOT / "reports")
        out_dir = out_dir / args.category
        if args.category == "morning":
            # Never an Official slot: research_freeze.py owns new canonical publication and the
            # historical reports/morning/<date>.md legacy slot is read-only.
            out_dir = out_dir / "preliminary"
            stem = f"{stem}_unfrozen_{now.strftime('%H%M')}PT"
            print("archive_report: morning category never writes the official slot; "
                  "archived as UNFROZEN preliminary (freeze via scripts/research_freeze.py)",
                  file=sys.stderr)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(out_dir, stem)
        dest.write_text(content)
        print(str(dest))
        return 0
    except Exception as exc:
        print(f"archive_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
