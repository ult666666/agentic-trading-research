#!/usr/bin/env python3
"""Persistent operator-attention marker — the "never silent again" primitive.

WHY
---
On 2026-07-22 the morning run died at 05:40 (Claude CLI not logged in). The only alert path was
notify() -> an osascript banner, which is EPHEMERAL (missed if the operator is asleep) and may not
even render from a launchd background context. Result: a hard failure was invisible for ~5 hours.

This primitive raises a DURABLE, hard-to-miss marker that survives until the underlying condition is
resolved, so a human-actionable failure (login expired, keychain locked) cannot hide:
  * data/runtime/health/ATTENTION_REQUIRED.md   — a human-readable, obvious top-of-health-dir file.
  * data/runtime/health/attention_required.json — machine-readable, so sim/sentry.py and the
    operator console surface it as a RED finding.
It also fires the existing osascript banner as a best-effort SECONDARY channel (never the only one).

MULTI-REASON (2026-07-28)
-------------------------
The marker now supports MULTIPLE SIMULTANEOUS reasons with deterministic priority ordering. On
2026-07-28 the 05:41 provider streak alert (`robinhood_collector_unavailable_streak`) was the only
marker on a day whose morning later failed completely (rc=89) — raising a second reason must never
erase the first, and a primary operational failure must outrank a secondary provider condition.
The top-level `reason`/`headline`/`actionable`/`detail`/`count` fields keep the v1.0 shape and
always mirror the HIGHEST-PRIORITY active reason (schema string is unchanged so existing readers —
sim/sentry.py, sentry_red_bridge — keep working); the additive `active_reasons` map carries every
concurrent reason. `clear --reason X` removes only X and re-primaries the rest.

CONTRACT
--------
- READ-ONLY toward the control plane and authority surfaces; writes ONLY the two health artifacts
  above. Grants no authority, invokes no broker, no model, no network. Stores NO credential value.
- `raise` is idempotent per (reason): re-raising refreshes the timestamp and bumps a count.
- `clear --reason X` removes X only; bare `clear` removes the whole marker; the successful next
  run clears its own reason so a resolved condition auto-clears without erasing the others.
- Fail-soft: any write error is reported in the return dict but never raises to the caller (a
  failed alert must not crash the runner's terminal path).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = "operator_attention_marker/1.0"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEALTH_DIR = ("data", "runtime", "health")
JSON_NAME = "attention_required.json"
MD_NAME = "ATTENTION_REQUIRED.md"

# Deterministic priority: lower rank = more primary. A primary operational failure (the day's
# research outcome) always outranks a secondary provider condition. Unknown reasons rank 50.
# Ranking rule: a condition that stops EVERY workflow outranks one that stops the day's
# research, which outranks one that degrades a single supplemental lane. The 2026-07-29..07-31
# outage inverted exactly this — a supplemental Robinhood read-path streak was the loudest
# artifact for three days while nothing at all was running.
REASON_PRIORITY = {
    "integrity_manifest_failed": 0,
    "run_lock_unsafe": 1,
    "release_provenance_failed": 2,
    "automation_outage_persistent": 3,
    "claude_login_required": 5,
    "morning_run_failed": 10,
    "attention_artifact_stale": 15,
    "journal_artifact_missing": 20,
    # A Buy verifier dead across CONSECUTIVE trading days is a different condition from one bad
    # session, and is ranked accordingly (2026-08-04, operator-ratified). It sits just below
    # `morning_run_failed`: a day on which nothing ran at all still outranks a day whose
    # research ran without its verifier.
    "robinhood_collector_unavailable_persistent": 11,
    "battery_unattended_risk": 30,
    "robinhood_collector_unavailable_streak": 40,
}
DEFAULT_PRIORITY = 50
MAX_ACTIVE_REASONS = 8
# Severity is DERIVED from the priority, never supplied by a caller: a whole-system blocker is
# critical by construction and cannot be downgraded by whoever happens to raise it. The ladder
# for a duration-dependent condition is expressed as TWO registered reasons with different
# priorities (see robinhood_collector_unavailable_{streak,persistent}) so the caller still only
# reports measured facts and never picks a severity.
CRITICAL_PRIORITY_MAX = 11
SEVERITIES = ("critical", "warning")
PT = "America/Los_Angeles"

# Single source of truth for run-lock operator guidance, keyed by `run_lock.inspect` status.
# `action` is either "quarantine" (the operator may move it aside) or "never_quarantine".
# scripts/test_run_lock_guidance_consistency.py asserts the "quarantine" set is exactly
# run_lock.QUARANTINABLE, so this text can never tell an operator to run a command the CLI is
# required to refuse.
RUN_LOCK_STATUS_ACTIONS = {
    "valid_live": {
        "action": "never_quarantine",
        "text": "valid_live  -> a real run owns it. Wait, or identify the holder: ps -p <pid>.",
    },
    "valid_dead": {
        "action": "never_quarantine",
        "text": "valid_dead  -> a strict lock whose owner is gone. The next scheduled run reclaims it automatically. No action.",
    },
    "legacy_dead": {
        "action": "quarantine",
        "text": "legacy_dead -> the next scheduled run reclaims it and preserves the evidence. No action is needed; to clear it WITHOUT starting a run, this is the ONLY status the operator quarantine accepts.",
    },
    "malformed": {
        "action": "never_quarantine",
        "text": "malformed   -> NOT recognised, and NOT quarantinable. Diagnose by hand before touching it: ls -laR .run.lock and identify the recorded pid.",
    },
    "symlink": {
        "action": "never_quarantine",
        "text": "symlink     -> NOT quarantinable. A symlinked lock means something outside this repository created it; find that writer first.",
    },
    "ambiguous": {
        "action": "never_quarantine",
        "text": "ambiguous   -> liveness is unknown (EPERM) or the lock changed while being read. NOT quarantinable; establish who owns the pid first.",
    },
}


def _run_lock_actionable() -> list:
    """Build the run_lock guidance from the table above, in deterministic status order."""
    lines = [
        "This is the 2026-07-29..07-31 failure mode: one unreclaimable lock silently took down three consecutive sessions.",
        "Read the exact classification and reason FIRST (never delete the lock):",
        "  cd /Users/prince/Projects/trading-system && python3 scripts/lib/run_lock.py --root . inspect",
    ]
    lines.extend(RUN_LOCK_STATUS_ACTIONS[status]["text"]
                 for status in sorted(RUN_LOCK_STATUS_ACTIONS))
    lines.extend([
        "The operator quarantine MOVES the lock aside as permanent evidence and never deletes it:",
        "  cd /Users/prince/Projects/trading-system && python3 scripts/lib/run_lock.py --root . quarantine --confirm",
        "It REFUSES every status above that is marked NOT quarantinable, so a wrong guess fails closed rather than destroying forensic state.",
        "Never use rm -rf on the lock: it destroys the only artifact that explains a missed session.",
        "This marker clears automatically on the next run that acquires the lock.",
    ])
    return lines


_KNOWN = {
    "claude_login_required": {
        "headline": "Claude CLI login expired — the unattended morning run cannot authenticate.",
        "actionable": [
            "Unlock the macOS login keychain (it locks on reboot/sleep/password-desync):",
            "  security unlock-keychain ~/Library/Keychains/login.keychain-db",
            "Re-authenticate the CLI:",
            "  claude auth login   &&   claude auth status   (expect loggedIn: true)",
            "Durable fix so this stops recurring (bypasses the keychain):",
            "  claude setup-token   -> put CLAUDE_CODE_OAUTH_TOKEN in git-ignored secrets/, not a tracked plist",
        ],
    },
    "battery_unattended_risk": {
        "headline": "Machine is ON BATTERY — unattended trading runs are at risk (idle-sleep/drain).",
        "actionable": [
            "Reconnect AC power now (on battery this Mac idle-sleeps within ~1 minute outside the 04:56-07:10 hold).",
            "Check charger/cable/outlet — 2026-07-14's unnoticed drain (3% by 09:09 PT) cost a full trading day.",
            "Verify the wake schedule survived:  pmset -g sched   (expect wakeorpoweron weekdays 04:55).",
            "This marker clears automatically at the next watchdog tick on AC power.",
        ],
    },
    "integrity_manifest_failed": {
        "headline": "Integrity manifest FAILED — every scheduled workflow is fail-closed (exit 78); no research runs until it is re-signed.",
        "actionable": [
            "Inspect the drift read-only:  python3 scripts/build_manifest.py --check",
            "Review each drifted file (git diff <path>) — confirm the changes are intended before blessing them.",
            "Re-sign after review:  python3 scripts/build_manifest.py   (then commit data/MANIFEST.json per the ceremony).",
            "This marker clears automatically at the next watchdog tick once --check passes.",
        ],
    },
    "journal_artifact_missing": {
        "headline": "A journal event claims a contract-valid report that is MISSING on disk.",
        "actionable": [
            "Read the reconciliation output in logs/automation.log (journal_disk_reconcile.py JSON).",
            "An audited-run quarantine likely holds the preserved bytes — inspect it before deciding anything.",
            "Adjudicate: restore the report from quarantine (operator action), OR record the case in",
            "  data/runtime/health/journal_disk_reconcile_ack.json  ({\"acknowledged_report_paths\": [...]}).",
            "Never rewrite or backfill the append-only journal itself.",
        ],
    },
    "morning_run_failed": {
        "headline": "The unattended MORNING RESEARCH RUN FAILED — no report was published for the target session.",
        "actionable": [
            "Read the exact failure class + discriminating detail:",
            "  cat data/runtime/health/<target>_run_status.json   (failure_class / failure_detail)",
            "Read the run timeline and the trusted outer-freeze reason:",
            "  cat data/runtime/health/<target>_timeline.json   &&   tail -40 logs/errors.log",
            "A labeled Preliminary checkpoint may exist under reports/morning/preliminary/ — it is NOT official.",
            "NO ORDER WAS PLACED. Nothing became Official; the ledger was not incremented.",
            "This marker clears automatically on the next successful morning terminal.",
        ],
    },
    "run_lock_unsafe": {
        "headline": "The repository RUN LOCK is in an unsafe state - EVERY scheduled workflow will exit 75 until it clears, and it cannot clear itself.",
        "actionable": _run_lock_actionable(),
    },
    "release_provenance_failed": {
        "headline": "Runtime PROVENANCE FAILED - the code about to run is not the code the release snapshot describes.",
        "actionable": [
            "On 2026-07-29 uncommitted control code ran in production for three days and caused the outage it was meant to prevent.",
            "Read the exact drift:",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/runtime_provenance.py --json",
            "Wrong branch      -> check out the production branch before the next scheduled session.",
            "Modified runtime  -> review with git diff, then commit or revert. Never leave control code uncommitted.",
            "Untracked shadow  -> an untracked file is shadowing a tracked runtime path; remove or track it.",
            "Stale snapshot    -> re-bind after the final commit:",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/release_snapshot.py create",
            "This marker clears automatically once provenance passes.",
        ],
    },
    "automation_outage_persistent": {
        "headline": "A scheduled workflow has now MISSED ITS SLO ON CONSECUTIVE TRADING SESSIONS - this is a broken system, not a bad morning.",
        "actionable": [
            "Read which workflows and how many consecutive sessions:",
            "  cd /Users/prince/Projects/trading-system && python3 -c \"import json;d=json.load(open('data/runtime/health/autonomy_service_alert.json'));print(json.dumps(d['slo_persistence'],indent=2))\"",
            "Then read the exit reason for the most recent attempt:",
            "  tail -60 logs/errors.log   &&   tail -40 logs/automation.log",
            "A repeated `runner_lock_contention` (exit 75) means the run lock never cleared - see the run_lock_unsafe guidance.",
            "A repeated `data_readiness_receipt_unavailable` (exit 78) is downstream of intake; fix intake first.",
            "NO ORDER WAS PLACED and nothing became Official; the missed sessions stay missed and are never backfilled.",
            "This marker clears automatically on the next session whose SLOs are met.",
        ],
    },
    "attention_artifact_stale": {
        "headline": "This operator marker is STALE - it describes an older session and must not be trusted for the current one.",
        "actionable": [
            "The marker was last refreshed for a session earlier than the one now being prepared.",
            "Re-derive current state instead of reading this file:",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/next_session_readiness.py",
            "Then re-check the durable marker:",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/auth_attention.py status",
            "A stale marker usually means the watchdog stopped running - verify the schedule before anything else.",
        ],
    },
    "robinhood_collector_unavailable_streak": {
        "headline": "Robinhood read-only collector failed on a TRADING session - the Buy verifier produced no usable read.",
        "actionable": [
            "One failed trading day. If the next session also fails this escalates to CRITICAL (robinhood_collector_unavailable_persistent).",
            "Probe the LIVE namespace first - a connector RENAME is the known cause and broke this lane twice (2026-07-22 and again on 2026-08-04, in the opposite direction):",
            "  claude mcp list",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/mcp_registry_probe.py",
            "Read the deterministic availability classification for the exact failing hop:",
            "  cat data/runtime/health/<target>_rh_availability.json",
            "Bind the structural audit to the measured registry (this is the check that was missing; the bare validator returned PASS throughout the 2026-07-22..08-04 outage):",
            "  python3 scripts/validate_broker_permissions.py --require-live",
            "Re-run the collector by hand and read the emitted status JSON:",
            "  bash scripts/run_robinhood_readonly_collector.sh",
            "Scope: THIS collector outage by itself blocks Buy only. It says NOTHING about the rest of the morning run — check the morning workflow outcome separately before concluding research was unaffected.",
            "This marker clears automatically on the next session whose read path resolves.",
        ],
    },
    "robinhood_collector_unavailable_persistent": {
        "headline": "CRITICAL: Robinhood Buy verifier has produced NO usable read on two or more consecutive TRADING sessions - the read path is BROKEN, not merely offline.",
        "actionable": [
            "This is the 2026-07-22..2026-08-04 failure mode: the read path was dead for ten trading sessions while every static gate reported PASS and the marker stayed at WARNING.",
            "Buy is blocked and next-session readiness is withheld until a real read succeeds. Nothing here authorizes an order; execution remains permanently disabled.",
            "Read the MEASURED registry attestation first - it names the failing hop deterministically:",
            "  cd /Users/prince/Projects/trading-system && python3 scripts/mcp_registry_probe.py --json",
            "  claude mcp list",
            "If the classification is account_connectors_invisible_to_credential, this is an AUTHENTICATION blocker, NOT a rename. scripts/lib/common.sh exports CLAUDE_CODE_OAUTH_TOKEN from secrets/claude_oauth_token, and under that credential `claude mcp list` returns ONLY local stdio servers - every claude.ai connector is invisible. Reproduce in one line:",
            "  env -u CLAUDE_CODE_OAUTH_TOKEN claude mcp list | wc -l   # vs. with it set",
            "Do NOT edit a namespace constant for this. It is a credential decision for the operator: the token exists so unattended launchd runs do not depend on the macOS login keychain, which locked and killed the 2026-07-22 morning run.",
            "If the classification is namespace_missing / retired_namespace_present, the connector was RENAMED. Update the single positive authority and NOTHING else:",
            "  scripts/lib/broker_namespace_policy.py  ->  ROBINHOOD_LIVE_NAMESPACE",
            "Then re-derive every consumer and re-sign, off the trading window:",
            "  python3 scripts/validate_broker_permissions.py --require-live",
            "  python3 -m pytest scripts/test_broker_namespace_policy.py scripts/test_robinhood_verifier.py -q",
            "  python3 scripts/build_manifest.py",
            "ACCEPTANCE: a namespace change is NOT landed until a collector receipt shows broker_read_attempt_total > 0. An internal classifier agreeing with itself is not evidence - that is exactly how the 2026-07-27 remediation was accepted while the lane stayed dead.",
            "  bash scripts/run_robinhood_readonly_collector.sh",
            "This marker clears automatically on the next session whose read path resolves.",
        ],
    },
}


def _health_dir(root: Path) -> Path:
    return Path(root).resolve().joinpath(*HEALTH_DIR)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".att-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _banner(headline: str) -> None:
    # The headline can embed a caller-supplied --reason (see _known()'s fallback), so it must be
    # escaped before entering an AppleScript string literal: an unescaped double quote breaks out
    # of the literal, and AppleScript reaches `do shell script` (2026-07-28 review D16). Both
    # subprocess calls are bounded.
    safe = headline[:180].replace("\\", "").replace('"', "'").replace("\n", " ").replace("\r", " ")
    try:
        if subprocess.run(["which", "osascript"], capture_output=True,
                          timeout=10).returncode == 0:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "Trading System — ACTION REQUIRED"'],
                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _priority(reason: str) -> int:
    return REASON_PRIORITY.get(reason, DEFAULT_PRIORITY)


def _severity(reason: str) -> str:
    """Deterministic, caller-independent severity for one reason."""
    return "critical" if _priority(reason) <= CRITICAL_PRIORITY_MAX else "warning"


def _pt_text(value: datetime) -> str:
    try:
        return value.astimezone(ZoneInfo(PT)).isoformat()
    except Exception:      # pragma: no cover - tzdata missing is not worth failing a marker
        return value.isoformat()


def _session_key(value) -> str:
    """A sortable session token; the empty string sorts before every real date."""
    return value if isinstance(value, str) and DATE_RE.fullmatch(value) else ""


def is_stale(document: dict, required_session: str) -> bool:
    """True when the marker cannot speak for `required_session`.

    A marker raised for an EARLIER session says nothing about the one about to run. Callers
    treat this as `attention_artifact_stale` rather than trusting the old text — the exact
    failure of 2026-07-29..07-31, where a two-day-old connector reason stayed the loudest
    artifact throughout a whole-system outage.
    """
    if not isinstance(document, dict) or not DATE_RE.fullmatch(required_session or ""):
        return True
    covered = _session_key(document.get("valid_through_session")) or _session_key(
        document.get("target_session_date"))
    return covered < required_session


def _known(reason: str) -> dict:
    return _KNOWN.get(reason, {
        "headline": f"Operator attention required: {reason}",
        "actionable": ["Investigate the referenced condition; no automated fix is available."],
    })


def _load_active(json_path: Path) -> dict[str, dict]:
    """Read the current active-reason map, upgrading a v1 single-reason document in place."""
    try:
        prior = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(prior, dict):
        return {}
    active = prior.get("active_reasons")
    if isinstance(active, dict) and active:
        rows = {reason: dict(row) for reason, row in active.items()
                if isinstance(reason, str) and isinstance(row, dict)}
        for row in rows.values():
            # Upgrade a pre-currency row in place so a v1 marker on disk stays readable.
            row.setdefault("source_run_id", "")
            row.setdefault("evidence_refs", [])
        return rows
    reason = prior.get("reason")
    if isinstance(reason, str) and reason:
        return {reason: {
            "target_session_date": prior.get("target_session_date") or "",
            "detail": prior.get("detail") or "",
            "count": int(prior.get("count") or 1),
            "first_raised_utc": prior.get("first_raised_utc") or prior.get("last_raised_utc"),
            "last_raised_utc": prior.get("last_raised_utc"),
        }}
    return {}


def _primary(active: dict[str, dict]) -> str:
    return sorted(active, key=lambda reason: (_priority(reason), reason))[0]


def _write_marker(root: Path, active: dict[str, dict], now_utc: datetime) -> dict:
    directory = _health_dir(root)
    json_path = directory / JSON_NAME
    md_path = directory / MD_NAME
    primary = _primary(active)
    primary_row = active[primary]
    known = _known(primary)
    ordered = sorted(active, key=lambda reason: (_priority(reason), reason))
    sessions = [_session_key(active[reason].get("target_session_date")) for reason in ordered]
    document = {
        "schema": SCHEMA,
        "status": "attention_required",
        # v1-compatible mirror of the HIGHEST-PRIORITY active reason (sentry/console readers).
        "reason": primary,
        "target_session_date": primary_row.get("target_session_date") or "",
        "headline": known["headline"],
        "actionable": known["actionable"],
        "detail": primary_row.get("detail") or "",
        "count": int(primary_row.get("count") or 1),
        "first_raised_utc": primary_row.get("first_raised_utc"),
        "last_raised_utc": primary_row.get("last_raised_utc"),
        # Additive multi-reason state (2026-07-28): every concurrent reason, priority-ordered.
        "active_reasons": {reason: active[reason] for reason in ordered},
        "active_reason_order": ordered,
        # Additive currency block (2026-07-31): a reader must be able to tell a live marker
        # from a two-day-old one WITHOUT consulting anything else.
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_pt": _pt_text(now_utc),
        "severity": _severity(primary),
        "severity_by_reason": {reason: _severity(reason) for reason in ordered},
        "source_run_id": primary_row.get("source_run_id") or "",
        "valid_through_session": max(sessions) if sessions else "",
        "evidence_refs": sorted({
            reference
            for reason in ordered
            for reference in (active[reason].get("evidence_refs") or [])
            if isinstance(reference, str) and reference
        })[:16],
        "read_only_toward_control_plane": True,
        "execution_enabled": False,
    }
    errors = []
    try:
        _atomic_write(json_path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        errors.append(f"json_write:{type(exc).__name__}")
    lines = [
        "# ⚠️  TRADING SYSTEM — ACTION REQUIRED",
        "",
        f"- severity: **{document['severity'].upper()}**",
        f"- generated (PT): {document['generated_at_pt']}",
        f"- generated (UTC): {document['generated_at_utc']}",
        f"- valid through session: `{document['valid_through_session'] or 'n/a'}`",
        f"- source run: `{document['source_run_id'] or 'n/a'}`",
        "",
        "> Reasons are listed HIGHEST PRIORITY FIRST. A condition that stops every workflow",
        "> outranks one that degrades a single supplemental data lane.",
        "",
    ]
    for index, reason in enumerate(ordered):
        row = active[reason]
        reason_known = _known(reason)
        prefix = "" if index == 0 else "## Also active (lower priority)\n\n"
        lines.append(f"{prefix}**{reason_known['headline']}**")
        lines.append("")
        lines.append(f"- reason: `{reason}`")
        lines.append(f"- target session: `{row.get('target_session_date') or 'n/a'}`")
        lines.append(f"- last raised (UTC): {row.get('last_raised_utc')}")
        lines.append(f"- times raised: {row.get('count')}")
        lines.append("")
        lines.append("### What to do")
        lines.append("")
        lines.extend(
            f"    {step}" if step.startswith(("  ", "security", "claude")) else f"- {step}"
            for step in reason_known["actionable"])
        lines.append("")
    if document["evidence_refs"]:
        lines.append("## Evidence")
        lines.append("")
        lines.extend(f"- `{reference}`" for reference in document["evidence_refs"])
        lines.append("")
    lines.extend([
        "This file was written by the automation because one or more hard, human-actionable",
        "conditions affected an unattended run. Each reason clears independently when resolved.",
        "NO ORDER WAS PLACED — this system is research-only and execution is permanently disabled.",
        "",
    ])
    try:
        _atomic_write(md_path, "\n".join(lines))
    except OSError as exc:
        errors.append(f"md_write:{type(exc).__name__}")
    return {"ok": not errors, "reason": primary, "active_reasons": ordered,
            "count": document["count"], "json_path": str(json_path),
            "md_path": str(md_path), "errors": errors}


def raise_attention(root: Path, *, reason: str, target: str = "", now_utc: datetime | None = None,
                    detail: str = "", source_run_id: str = "",
                    evidence_refs: list[str] | None = None) -> dict:
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    json_path = _health_dir(root) / JSON_NAME
    active = _load_active(json_path)
    prior = active.get(reason) or {}
    # Bound the map so a caller looping over novel reasons cannot grow the marker (and the
    # per-reason markdown sections) without limit (2026-07-28 review D17). Eviction is
    # deterministic: the lowest-priority (then lexically last) reason is dropped.
    if reason not in active and len(active) >= MAX_ACTIVE_REASONS:
        evictable = sorted(active, key=lambda item: (_priority(item), item))
        for candidate in reversed(evictable):
            if _priority(candidate) >= _priority(reason):
                del active[candidate]
                break
        else:
            return {"ok": True, "reason": _primary(active), "active_reasons": sorted(active),
                    "count": 0, "json_path": str(json_path),
                    "md_path": str(_health_dir(root) / MD_NAME),
                    "errors": ["active_reason_cap_reached"]}
    active[reason] = {
        "target_session_date": target or prior.get("target_session_date") or "",
        "detail": detail or prior.get("detail") or "",
        "count": int(prior.get("count") or 0) + 1,
        "first_raised_utc": prior.get("first_raised_utc") or now_utc.isoformat(),
        "last_raised_utc": now_utc.isoformat(),
        "source_run_id": source_run_id or prior.get("source_run_id") or "",
        "evidence_refs": sorted({
            reference for reference in
            list(evidence_refs or []) + list(prior.get("evidence_refs") or [])
            if isinstance(reference, str) and reference
        })[:8],
    }
    result = _write_marker(root, active, now_utc)
    _banner(_known(result["reason"])["headline"])
    return result


def clear_attention(root: Path, *, reason: str | None = None) -> dict:
    directory = _health_dir(root)
    json_path = directory / JSON_NAME
    md_path = directory / MD_NAME
    if reason is not None:
        active = _load_active(json_path)
        if reason not in active:
            kept = _primary(active) if active else None
            return {"ok": True, "cleared": False, "kept_reason": kept}
        del active[reason]
        if active:
            # Other conditions remain: re-primary and keep the marker (never erase a
            # concurrent issue merely because another one was resolved first).
            result = _write_marker(root, active, datetime.now(timezone.utc))
            return {"ok": result["ok"], "cleared": True,
                    "kept_reason": result["reason"],
                    "active_reasons": result["active_reasons"]}
    cleared = False
    for path in (json_path, md_path):
        try:
            path.unlink()
            cleared = True
        except OSError:
            pass
    return {"ok": True, "cleared": cleared}


def status(root: Path, *, require_session: str = "") -> dict:
    json_path = _health_dir(root) / JSON_NAME
    try:
        if json_path.is_file():
            doc = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                active = doc.get("active_reasons")
                reasons = (sorted(active, key=lambda r: (_priority(r), r))
                           if isinstance(active, dict) and active
                           else ([doc.get("reason")] if doc.get("reason") else []))
                result = {"attention_required": True, "reason": doc.get("reason"),
                          "active_reasons": reasons,
                          "severity": doc.get("severity") or _severity(str(doc.get("reason"))),
                          "generated_at_utc": doc.get("generated_at_utc"),
                          "valid_through_session": doc.get("valid_through_session") or "",
                          "count": doc.get("count"),
                          "last_raised_utc": doc.get("last_raised_utc")}
                if require_session:
                    stale = is_stale(doc, require_session)
                    result["required_session"] = require_session
                    result["stale"] = stale
                    if stale:
                        # Never let old text speak for a session it predates.
                        result["reason_codes"] = ["attention_artifact_stale"]
                return result
    except (OSError, ValueError):
        # A marker that exists but cannot be parsed is NOT an absence of trouble.
        if json_path.exists():
            return {"attention_required": True, "reason": "attention_artifact_stale",
                    "active_reasons": ["attention_artifact_stale"], "severity": "warning",
                    "reason_codes": ["attention_artifact_malformed"], "stale": True}
    result = {"attention_required": False}
    if require_session:
        # An ABSENT marker is a legitimate CLEAR state, not a stale one.
        result.update({"required_session": require_session, "stale": False})
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Raise/clear a persistent operator-attention marker.")
    ap.add_argument("action", choices=["raise", "clear", "status"])
    # parents[1] == the repo root for a file at scripts/<name>.py; parents[2] was an
    # off-by-one that pointed markers at the repo PARENT, invisible to the sentry (O9).
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--reason", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--detail", default="")
    ap.add_argument("--source-run-id", default="", dest="source_run_id")
    ap.add_argument("--evidence", action="append", default=[],
                    help="Repeatable repo-relative receipt/evidence path.")
    ap.add_argument("--require-session", default="", dest="require_session",
                    help="status only: report attention_artifact_stale below this session.")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.action == "raise":
        if not args.reason:
            print(json.dumps({"ok": False, "error": "reason_required"})); return 2
        print(json.dumps(raise_attention(root, reason=args.reason, target=args.target,
                                         detail=args.detail,
                                         source_run_id=args.source_run_id,
                                         evidence_refs=args.evidence),
                         indent=2, sort_keys=True))
        return 0
    if args.action == "clear":
        print(json.dumps(clear_attention(root, reason=args.reason or None), indent=2, sort_keys=True))
        return 0
    report = status(root, require_session=args.require_session)
    print(json.dumps(report, indent=2, sort_keys=True))
    # Exit 3 marks "a marker exists but cannot speak for the session you asked about", so a
    # shell caller can branch on staleness without parsing JSON.
    return 3 if report.get("stale") else 0


if __name__ == "__main__":
    raise SystemExit(main())
