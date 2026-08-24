#!/usr/bin/env python3
"""scripts/quant — OFFLINE quant research lab (Layer 3 skeleton). DIAGNOSTICS ONLY.

Safety contract (constitution §3a — decision path is locked):
  * Nothing in this package feeds production scoring, gates, classifications, or reports.
    It is a research lab, not a trading engine; execution remains permanently disabled.
  * Every output embeds DIAGNOSTIC_FIELDS (mirrors scripts/decision_path.diagnostic_record)
    so no record can ever be counted toward win rate / expectancy / official replay outcomes.
  * Activation of ANY finding requires backlog category D approval (replay evidence +
    explicit user approval + post-activation review); experiment manifests record this.
  * Fail-closed: missing / stale / malformed / insufficient data => {"error": ...} and a
    non-zero exit — never a fabricated value, never a silent partial pass.
  * std-lib only; the only permitted archive is data/replay_bars.json (read-only).
"""

# Mirror of scripts/decision_path.diagnostic_record exclusion fields, plus the lab-wide
# disclaimers. Constraint: these keys/values must stay in lockstep with decision_path.py
# so downstream metric filters exclude lab output by the exact same predicate.
DIAGNOSTIC_FIELDS = {
    "actionable": False,
    "record_type": "non_actionable_diagnostic",
    "excluded_from_performance": True,
    "excluded_from_win_rate": True,
    "excluded_from_expectancy": True,
    "excluded_from_official_replay_outcomes": True,
    "diagnostic_only": True,
    "feeds_production": False,
    "note": ("Offline quant research lab diagnostic. NOT a proposed Buy; NEVER feeds "
             "production scoring/gates; excluded from all performance metrics."),
}
