#!/usr/bin/env python3
"""Options-lane capability receipt builder/validator (v0). Standard library only.

A capability that has not been positively verified is UNKNOWN, and UNKNOWN is BLOCKING —
nothing is ever inferred as available (mirrors scripts/lib/fractional_policy.py's verified-
record discipline). The receipt carries booleans/status strings ONLY; account-private data
(balances, account numbers, approval-level detail) must never enter it, and the builder
scrubs/refuses rather than passing unknown fields through.

Static facts (read tools present, mutation tools denied) are derived from the repo's own
records: data/broker_tool_namespace.json and .claude/settings.json plus the broker-tool hook.
Live facts (quote/greek field verification, regular-hours probe, account context) require an
attended read-only probe artifact; absent that, they stay UNKNOWN and the lane fails closed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_VERSION = "options_capability_receipt/1.0"

# Every option-mutation basename that must be deny-listed in every recognized broker
# namespace that exposes it. Unknown names are handled separately by the fail-closed hook.
REQUIRED_DENIED_BASENAMES = (
    "place_option_order",
    "cancel_option_order",
    "review_option_order",
    "exercise_option",
    "cancel_option_exercise",
    "add_option_to_watchlist",
    "remove_option_from_watchlist",
)
# DENY-side namespace set: every Robinhood identity that has ever existed must carry the option
# mutation denies, so a retired name resurfacing cannot reopen an execution surface. Sourced from
# the single namespace authority rather than re-literalled here (the 2026-08-04 drift class).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import broker_namespace_policy as _bnp  # noqa: E402

ROBINHOOD_NAMESPACES = tuple(_bnp.ROBINHOOD_ARCHIVE_NAMESPACES)
REQUIRED_OPTION_READ_BASENAMES = ("get_option_chains", "get_option_quotes",
                                  "get_option_instruments")

_TRISTATE = (True, False, "UNKNOWN")
_REQUIRED_FIELDS = (
    "schema_version", "generated_utc", "read_tools_present", "quote_fields_verified",
    "greek_fields_present", "greek_units_verified", "account_options_approval_verified",
    "account_type_verified", "settled_cash_field_verified",
    "regular_hours_live_probe_completed", "mutation_tools_denied", "status", "blockers",
    "provenance",
)
_PRIVATE_FRAGMENTS = ("account_number", "balance", "cash_amount", "email", "token", "ssn")


def _load_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _static_read_tools_present(root: Path) -> tuple[bool | str, list[str]]:
    namespace = _load_json(root / "data" / "broker_tool_namespace.json")
    if namespace is None:
        return "UNKNOWN", ["broker_tool_namespace_unreadable"]
    servers = namespace.get("servers")
    if not isinstance(servers, dict):
        return "UNKNOWN", ["broker_tool_namespace_malformed"]
    read_names = set()
    for server in servers.values():
        tools = server.get("tools", {}) if isinstance(server, dict) else {}
        for name in tools.get("read_only", []) if isinstance(tools, dict) else []:
            read_names.add(str(name).split("__")[-1])
    missing = [base for base in REQUIRED_OPTION_READ_BASENAMES if base not in read_names]
    if missing:
        return False, [f"option_read_tool_absent:{base}" for base in missing]
    return True, []


def _static_mutations_denied(root: Path) -> tuple[bool | str, list[str]]:
    settings = _load_json(root / ".claude" / "settings.json")
    if settings is None:
        return "UNKNOWN", ["settings_json_unreadable"]
    deny = settings.get("permissions", {}).get("deny", [])
    if not isinstance(deny, list):
        return "UNKNOWN", ["settings_deny_malformed"]
    deny_set = {str(item) for item in deny}
    blockers: list[str] = []
    for namespace in ROBINHOOD_NAMESPACES:
        for base in REQUIRED_DENIED_BASENAMES:
            if f"mcp__{namespace}__{base}" not in deny_set:
                blockers.append(f"mutation_not_denied:{namespace}:{base}")
    hook_path = root / ".claude" / "hooks" / "protect_broker_tools.py"
    if not hook_path.is_file():
        blockers.append("broker_tool_hook_missing")
    else:
        hooks = settings.get("hooks", {}).get("PreToolUse", [])
        registered = any("protect_broker_tools" in json.dumps(entry) for entry in hooks) \
            if isinstance(hooks, list) else False
        if not registered:
            blockers.append("broker_tool_hook_not_registered")
    return (False, blockers) if blockers else (True, [])


def _tristate_from_probe(probe: dict | None, key: str) -> bool | str:
    if not isinstance(probe, dict):
        return "UNKNOWN"
    value = probe.get(key)
    return value if isinstance(value, bool) else "UNKNOWN"


def build_capability_receipt(root: Path | None = None,
                             live_probe: dict | None = None) -> dict:
    """Build the receipt. `live_probe` is an OPTIONAL, already-sanitized boolean summary of an
    attended regular-hours read-only probe; it may contain ONLY boolean fields named below.
    Any private-looking key refuses the probe outright (fail closed, never scrub-and-trust)."""
    root = Path(root or _REPO_ROOT)
    blockers: list[str] = []
    if isinstance(live_probe, dict):
        for key in live_probe:
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _PRIVATE_FRAGMENTS):
                live_probe = None
                blockers.append("live_probe_refused_private_content")
                break

    read_present, read_blockers = _static_read_tools_present(root)
    blockers.extend(read_blockers)
    denied, deny_blockers = _static_mutations_denied(root)
    blockers.extend(deny_blockers)

    fields = {
        "quote_fields_verified": _tristate_from_probe(live_probe, "quote_fields_verified"),
        "greek_fields_present": _tristate_from_probe(live_probe, "greek_fields_present"),
        "greek_units_verified": _tristate_from_probe(live_probe, "greek_units_verified"),
        "account_options_approval_verified":
            _tristate_from_probe(live_probe, "account_options_approval_verified"),
        "account_type_verified": _tristate_from_probe(live_probe, "account_type_verified"),
        "settled_cash_field_verified":
            _tristate_from_probe(live_probe, "settled_cash_field_verified"),
        "regular_hours_live_probe_completed":
            _tristate_from_probe(live_probe, "regular_hours_live_probe_completed"),
    }
    for name, value in fields.items():
        if value is not True:
            blockers.append(f"capability_unverified:{name}")
    if read_present is not True:
        blockers.append("capability_unverified:read_tools_present")
    if denied is not True:
        blockers.append("capability_unverified:mutation_tools_denied")

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "read_tools_present": read_present,
        "mutation_tools_denied": denied,
        **fields,
        "status": "READY" if not blockers else "BLOCKED",
        "blockers": sorted(set(blockers)),
        "provenance": {
            "method": "static repo inspection (broker_tool_namespace.json, settings.json, "
                      "protect_broker_tools hook) + optional attended sanitized live probe",
            "inputs": ["data/broker_tool_namespace.json", ".claude/settings.json",
                       ".claude/hooks/protect_broker_tools.py"],
            "live_probe_note": None if live_probe is None
            else "sanitized boolean probe summary supplied",
        },
    }
    return receipt


def validate_receipt(receipt: object) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not isinstance(receipt, dict):
        return False, ["receipt_not_an_object"]
    for field in _REQUIRED_FIELDS:
        if field not in receipt:
            problems.append(f"missing_field:{field}")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        problems.append("wrong_schema_version")
    for field in ("read_tools_present", "quote_fields_verified", "greek_fields_present",
                  "greek_units_verified", "account_options_approval_verified",
                  "account_type_verified", "settled_cash_field_verified",
                  "regular_hours_live_probe_completed", "mutation_tools_denied"):
        if receipt.get(field) not in _TRISTATE:
            problems.append(f"invalid_tristate:{field}")
    status = receipt.get("status")
    if status not in ("READY", "BLOCKED"):
        problems.append("invalid_status")
    if status == "READY":
        for field in ("read_tools_present", "quote_fields_verified", "greek_fields_present",
                      "greek_units_verified", "account_options_approval_verified",
                      "account_type_verified", "settled_cash_field_verified",
                      "regular_hours_live_probe_completed", "mutation_tools_denied"):
            if receipt.get(field) is not True:
                problems.append(f"ready_with_unverified:{field}")
        if receipt.get("blockers"):
            problems.append("ready_with_blockers")
    scan = json.dumps(receipt).lower()
    for fragment in _PRIVATE_FRAGMENTS:
        # No legitimate receipt field name or value contains these fragments; any hit means
        # private content leaked into the receipt and the receipt is invalid.
        if fragment in scan:
            problems.append(f"private_content_detected:{fragment}")
    return (not problems), problems


def main() -> int:
    receipt = build_capability_receipt()
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
