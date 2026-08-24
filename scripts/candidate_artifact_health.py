#!/usr/bin/env python3
"""Structured-candidate ADOPTION / HEALTH checker (read-only observability, fail-closed).

Answers, deterministically, one question per target session: did the structured candidate path
(prompts/morning_scan.md step 4b -> freeze_candidates.py) actually ACTIVATE, or did the run
silently fall back to prose extraction / an empty artifact?

Reads ONLY:
  * data/runtime/research/<target>/candidates.json       (the freeze-time artifact)
  * data/runtime/research/<target>/freeze_receipt.json   (governance-v2 context)
  * data/research_ledger.json                            (optional cross-check, fail-soft)

This is OBSERVABILITY ONLY — not a trading gate, not activation authority. It rewrites no
ledger, touches no broker surface, and never modifies any input it reads; with --out it writes
EXACTLY ONE derived artifact (its own health JSON, atomic tempfile+os.replace, best-effort — a
write failure is recorded as a warning, never an error exit). Output always carries
diagnostic_only=true / execution_enabled=false. Health labels:

  healthy   source=structured_draft and the artifact validates (an EMPTY structured list on a
            No-Trade run is still healthy — the structured path activated as instructed)
  fallback  source=report_extractor_fallback (ok=true, but the synthesizer did not produce a
            usable candidates_draft.json — adoption gap worth investigating)
  empty     source=none (no structured draft AND no extractable report — expected for
            blackout/no-report runs)
  missing   candidates.json absent => ok=false (freeze crashed before emission, or predates
            the freeze_candidates integration)
  invalid   artifact unreadable / fails validate_artifact / or its canonical freeze completion
            (receipt, report, evidence, live source, authority, candidate quotes) cannot be
            re-anchored => ok=false (do not trust its contents)

Std-lib only. CLI:
  candidate_artifact_health.py --target YYYY-MM-DD [--root DIR] [--ledger PATH|-] [--out PATH]
Exit 0 when ok=true, 1 when ok=false, 2 on unexpected error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import freeze_candidates as fc  # noqa: E402

VALID_SOURCES = ("structured_draft", "report_extractor_fallback", "none", "unavailable")
HEALTH_LABELS = ("healthy", "fallback", "empty", "missing", "invalid")
ACTION_HINTS = {
    "healthy": "none — structured candidate path is active",
    "fallback": "synthesizer did not write a usable candidates_draft.json — check prompt "
                "step 4b compliance and the artifact warnings list",
    "empty": "no structured draft and no extractable report — expected for blackout/no-report "
             "runs; verify against the receipt classification",
    "missing": "candidates.json was never written — freeze may have crashed before emission or "
               "predates the freeze_candidates integration; check the freeze receipt and "
               "run timeline",
    "invalid": "artifact failed validation or does not match its freeze receipt — inspect "
               "data/runtime/research/<target>/candidates.json and do NOT trust its contents",
}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def check(target: str, root: Path | None = None, ledger_path: Path | None = None) -> dict:
    base = root or ROOT
    rdir = fc.research_dir(target, base)
    out = {
        "ok": False,
        "target_session_date": target,
        "artifact_present": False,
        "artifact_valid": False,
        "source": "unavailable",
        "candidate_count": None,
        "dropped_count": None,
        "warnings": [],
        "receipt_classification": None,
        "official_research": None,
        "buy_authorized": None,
        "health_label": "missing",
        "action_hint": ACTION_HINTS["missing"],
        # Hard framing — this is observability of a diagnostic pipeline, nothing more.
        "diagnostic_only": True,
        "execution_enabled": False,
        "activation_authority": False,
    }

    # Only the active governance-v2 receipt can bind candidate health. Legacy receipts remain
    # historical audit artifacts and never form an active fallback.
    v2_receipt_path = rdir / "freeze_receipt.json"
    receipt = _read_json(v2_receipt_path)
    receipt_errors: list[str] = []
    if isinstance(receipt, dict):
        receipt_errors = fc.validate_completion_binding_for_candidates(
            receipt, target, base)
    if isinstance(receipt, dict) and not receipt_errors:
        # Authority labels are copied only after canonical completion is independently proven.
        out["receipt_classification"] = receipt.get("classification")
        out["official_research"] = fc._receipt_is_official_research(receipt)
        out["buy_authorized"] = receipt.get("buy_authorized") is True
    else:
        if receipt_errors:
            out["warnings"].extend(
                f"receipt_completion:{error}" for error in receipt_errors[:20])
        else:
            receipt = None
            out["warnings"].append("freeze_receipt_missing_or_unreadable")

    art_path = fc.artifact_path(target, base)
    if not art_path.exists():
        return out  # missing: ok=false (fail closed — absence is never silent success)

    out["artifact_present"] = True
    if fc._has_symlink_component(art_path, base) or not art_path.is_file():
        out["health_label"] = "invalid"
        out["action_hint"] = ACTION_HINTS["invalid"]
        out["warnings"].append("artifact_symlink_or_nonfile")
        return out
    doc = _read_json(art_path)
    if doc is None:
        out["health_label"] = "invalid"
        out["action_hint"] = ACTION_HINTS["invalid"]
        out["warnings"].append("artifact_unreadable")
        return out

    errs = fc.validate_artifact(doc)
    if errs:
        out["health_label"] = "invalid"
        out["action_hint"] = ACTION_HINTS["invalid"]
        out["warnings"].extend(f"validate:{e}" for e in errs[:20])
        return out

    # Receipt cross-checks: both Preliminary and Official artifacts require the same shared
    # canonical completion proof before their contents are trusted. The payload must then be
    # re-derived from its immutable candidate source and exact receipt binding.
    if receipt is not None:
        mismatches = list(receipt_errors)
        mismatches.extend(fc.validate_against_receipt(doc, receipt, target, base))
        # Final shared re-anchor closes the validation/use gap for Preliminary as well as
        # Official: source bytes or the canonical receipt cannot change during payload checks
        # and still produce a healthy verdict.
        mismatches.extend(fc.validate_completion_binding_for_candidates(
            receipt, target, base))
    else:
        mismatches = ["freeze_receipt_missing_or_unreadable"]
    if mismatches:
        out["health_label"] = "invalid"
        out["action_hint"] = ACTION_HINTS["invalid"]
        out["warnings"].extend(mismatches)
        return out

    out["artifact_valid"] = True
    source = doc.get("source")
    out["source"] = source if source in VALID_SOURCES else "unavailable"
    out["candidate_count"] = doc.get("candidate_count")
    out["dropped_count"] = doc.get("dropped_count")
    out["warnings"].extend(str(w) for w in (doc.get("warnings") or [])[:20])

    if out["source"] == "structured_draft":
        out["health_label"], out["ok"] = "healthy", True
    elif out["source"] == "report_extractor_fallback":
        out["health_label"], out["ok"] = "fallback", True
    elif out["source"] == "none":
        out["health_label"], out["ok"] = "empty", True
    else:
        out["health_label"], out["ok"] = "invalid", False
        out["warnings"].append(f"unknown_source:{source}")
    out["action_hint"] = ACTION_HINTS[out["health_label"]]

    # Optional ledger cross-check (fail-soft observability, never flips health).
    lp = ledger_path if ledger_path is not None else (base / "data" / "research_ledger.json")
    if lp and lp.exists():
        ledger = _read_json(lp)
        entries = ledger.get("entries") if isinstance(ledger, dict) else None
        if isinstance(entries, list) and not any(
                isinstance(e, dict) and e.get("target_session_date") == target for e in entries):
            out["warnings"].append("no_research_ledger_entry_for_target")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Structured-candidate adoption/health checker "
                                             "(read-only observability).")
    ap.add_argument("--target", required=True, help="Target session date YYYY-MM-DD.")
    ap.add_argument("--root", help="Project root override (tests).")
    ap.add_argument("--ledger", help="Research ledger path override; '-' disables the check.")
    ap.add_argument("--out", help="Also write the health JSON to this path (atomic, "
                                  "best-effort — a write failure becomes a warning).")
    args = ap.parse_args(argv)
    try:
        ledger = None
        if args.ledger == "-":
            ledger = Path("/nonexistent-disabled")
        elif args.ledger:
            ledger = Path(args.ledger)
        out = check(args.target, root=Path(args.root) if args.root else None, ledger_path=ledger)
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
                out["warnings"].append(f"health_artifact_write_failed:{type(exc).__name__}")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["ok"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "health_label": "invalid",
                          "diagnostic_only": True, "execution_enabled": False,
                          "activation_authority": False}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
