# Provenance & Vendored-Asset Ledger

This project vendors deterministic scripts and reference docs from an Anthropic skill. They
are copied verbatim and **must not be silently overwritten** by a future skill update — any
update is a reviewed change (verify hashes, run `self_test.py`, log in `journal/system_changes.md`).

## Vendored skill

- **Skill name:** `disciplined-trading-suite` (anthropic-skills)
- **Source location (this machine):**
  `~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/<…>/skills/disciplined-trading-suite`
- **Retrieved:** 2026-06-22 (Phase 1)
- **Verified unmodified:** 2026-06-22 (Phase 2) — all files byte-identical to source (SHA-256 match)
- **Local modifications:** **none** to vendored files. Project-specific logic lives in
  separate files (`scripts/score_candidate.py`, `scripts/validate_market_day.py`,
  `scripts/validate_calendar_coverage.py`, `scripts/archive_report.py`,
  `scripts/validate_sources.py`, `scripts/build_manifest.py`, runners, hooks).

## Vendored file SHA-256 (truncated; full hashes in data/MANIFEST.json)

| File | SHA-256 (first 16) |
|---|---|
| scripts/lib/risk_gate.py | 89416c00327134b3… |
| scripts/lib/trade_score.py | 8af26f16790e1033… |
| scripts/lib/session_controller.py | 25615907d000e6c1… |
| scripts/lib/reconcile_order.py | 49cf15717842f0b8… |
| scripts/lib/journal_event.py | 72abb5b292be477b… |
| scripts/lib/self_test.py | 7cfed4335b95a964… |
| scripts/lib/references/*.md (9 files) | all identical to source |

## Update policy

1. Never auto-overwrite `scripts/lib/`. To update: diff against the new skill version,
   re-run `python3 scripts/lib/self_test.py` (must stay 12/12), re-run
   `python3 scripts/build_manifest.py`, and record the change (old/new hash, reason) in
   `journal/system_changes.md`.
2. Project-authored scripts are the integration layer and may evolve independently.
3. `data/MANIFEST.json` is the integrity baseline; regenerate it after any intentional change
   and diff the `critical_sha256` block to detect drift.

## Market-calendar provenance

`config/market_schedule.yaml` was cross-checked 2026-06-22 against the live
**FMP `marketHours / holidays-by-exchange` (NASDAQ)** endpoint: all 2026 full-closes and both
early closes (11-27, 12-24) matched. Annual refresh procedure: `prompts/calendar_refresh.md`.
