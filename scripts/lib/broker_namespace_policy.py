#!/usr/bin/env python3
"""Single authoritative source for approved brokerage namespace identity.

WHY
---
Robinhood's MCP server name has now changed TWICE, in opposite directions:

  * 2026-07-22  `claude_ai_RobinHood` -> `8941ceef-f601-4890-9727-e54e06fe07ff`
  * on/before 2026-08-04 the connector was re-exposed under its display name again, so the
    live server is `claude_ai_RobinHood` and the UUID no longer exists in the registry.

The 2026-07-27 remediation centralised the constant here — correct — but recorded the UUID as
the single LIVE namespace and forbade the display name from entering a live `--allowedTools`
list. When the name changed back, every live render named seven tools that do not exist, the
headless collector saw an empty Robinhood inventory, and the read path was silently dead from
2026-07-22 until 2026-08-04. Nothing in the chain ever compared the declared namespace against
the measured registry, and `scripts/validate_broker_permissions.py` reported PASS throughout.

2026-08-04 correction (operator-ratified):

  * `claude_ai_RobinHood` is the ONE canonical ACTIVE identity. Every positive path — live
    `--allowedTools` renders, prompt placeholders, config allowlists, hydrator surfaces — uses
    it and only it.
  * The UUID is RETIRED. It survives here solely so (a) archived receipts/event logs stay
    classifiable and (b) the retired-namespace mutation DENY entries in `.claude/settings.json`
    keep a name to bind to. It must never appear in a positive allow path again.
  * Declaring the live namespace is no longer sufficient. `scripts/mcp_registry_probe.py`
    MEASURES the registry (`claude mcp list` — deterministic, no model, no broker call) and
    `scripts/validate_broker_permissions.py --require-live` fails closed when the declared
    live namespace is missing, disconnected, unauthenticated, duplicated, or ambiguous.

SECURITY CONTRACT
-----------------
* A tool is approved only when BOTH its namespace AND its basename match. Basename alone never
  authorizes anything — `get_equity_quotes` under a hostile namespace is rejected.
* The approved namespace tuple is a closed set. An unknown namespace fails closed.
* This module grants READ identity only. It never lists, implies, or enables a mutation,
  order, account-action, or preview tool, and no consumer may use it to widen authority.
* Adding a namespace or a basename here is a policy change and moves every consumer's
  effective policy hash.
* Identity is NEVER resolved from an observed/self-reported tool inventory. Resolution runs one
  direction only: the declared live namespace must be PRESENT in the measured registry. A
  permissive "adopt whatever namespace exposes get_equity_quotes" resolver was reviewed and
  rejected (D13/D14) because it would authorize any connector that happens to expose the
  basename — exactly the defect class this module exists to eliminate.

Std-lib only. No I/O, no network, no broker call — pure identity mapping so it is safe to import
from hooks, validators, and audited child processes alike.
"""
from __future__ import annotations

import re

# Robinhood server names. New live invocations must use ONLY ROBINHOOD_LIVE_NAMESPACE.
#
# 2026-08-04 (second correction, operator-authorised under CLAUDE.md §10a): production no longer
# depends on the claude.ai-managed RobinHood connector at all. The unattended runner authenticates
# with CLAUDE_CODE_OAUTH_TOKEN, and that credential type does not carry claude.ai account
# connectors — measured, twice: 21 servers visible without the variable, 1 (local kalshi) with it.
# The supported path is a DIRECTLY CONFIGURED, project-local MCP server:
#
#   claude mcp add --transport http robinhood-trading --scope local \
#     https://agent.robinhood.com/mcp/trading
#   claude mcp login robinhood-trading
#
# so the live tool prefix is `mcp__robinhood-trading__`. BOTH previous identities
# (`claude_ai_RobinHood` and the 2026-07-22 UUID alias) are now RETIRED: they survive only to
# classify archived receipts and to bind their mutation DENY entries, and appear in no positive
# allow or configuration path.
#
# The declared prefix is never trusted on its own. scripts/mcp_registry_probe.py MEASURES the
# structured `system/init` tool inventory and fails closed unless the live namespace actually
# exposes the exact reviewed read basenames (see REQUIRED_LIVE_READ_BASENAMES below).
ROBINHOOD_LIVE_NAMESPACE = "robinhood-trading"
# Retained name for the primary retired identity (archived receipts + deny binding). The
# constant keeps its historical name so receipt-classification call sites do not churn.
ROBINHOOD_LEGACY_NAMESPACE = "claude_ai_RobinHood"
ROBINHOOD_UUID_ALIAS_NAMESPACE = "8941ceef-f601-4890-9727-e54e06fe07ff"
ROBINHOOD_LIVE_NAMESPACES: tuple[str, ...] = (ROBINHOOD_LIVE_NAMESPACE,)
# dict.fromkeys (not a literal tuple) so that if LIVE and a retired name are ever set to the same
# string the archive tuple degrades to one element instead of silently halving every APPROVED
# frozenset.
ROBINHOOD_ARCHIVE_NAMESPACES: tuple[str, ...] = tuple(dict.fromkeys(
    (ROBINHOOD_LIVE_NAMESPACE, ROBINHOOD_LEGACY_NAMESPACE, ROBINHOOD_UUID_ALIAS_NAMESPACE)
))
# Backward-compatible receipt-validation alias. Live call sites must use the explicitly named
# *_LIVE_* constants/helpers below.
ROBINHOOD_NAMESPACES: tuple[str, ...] = ROBINHOOD_ARCHIVE_NAMESPACES
# RETIRED = archive minus live. DERIVED, never hand-listed, so 'live' and 'legacy' are disjoint
# by construction and a future rename cannot make a retired namespace classify as live.
ROBINHOOD_RETIRED_NAMESPACES: tuple[str, ...] = tuple(
    namespace for namespace in ROBINHOOD_ARCHIVE_NAMESPACES
    if namespace not in ROBINHOOD_LIVE_NAMESPACES
)

# The seven read basenames the read-only collector is authorised to use. This is the SAME set
# reflected in scripts/validate_broker_permissions.py READ_ALLOWLIST for both namespaces.
COLLECTOR_READ_BASENAMES: frozenset[str] = frozenset({
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_orders",
    "get_equity_quotes",
    "get_equity_tradability",
    "get_realized_pnl",
})

# The two read basenames the candidate-quote hydrator lane may use. Deliberately a strict subset:
# the hydrator needs price + tradability only and must never reach account or position data.
HYDRATOR_READ_BASENAMES: frozenset[str] = frozenset({
    "get_equity_quotes",
    "get_equity_tradability",
})

_TOOL_RE = re.compile(r"^mcp__(?P<ns>[A-Za-z0-9_.-]+)__(?P<base>[A-Za-z0-9_.-]+)$")


def split_tool(tool_name: str) -> tuple[str | None, str | None]:
    """('namespace', 'basename') for a well-formed MCP tool name, else (None, None).

    Fails closed on anything that is not exactly one `mcp__<ns>__<base>` triple.
    """
    if not isinstance(tool_name, str):
        return None, None
    match = _TOOL_RE.match(tool_name)
    if match is None:
        return None, None
    return match.group("ns"), match.group("base")


def qualified(basename: str, namespaces: tuple[str, ...] = ROBINHOOD_NAMESPACES) -> tuple[str, ...]:
    """Every receipt/audit-qualified name for one basename, live namespace first."""
    return tuple(f"mcp__{namespace}__{basename}" for namespace in namespaces)


def live_qualified(basename: str) -> tuple[str, ...]:
    """The single fully-qualified name permitted in a new live invocation."""
    return qualified(basename, ROBINHOOD_LIVE_NAMESPACES)


def approved_tools(basenames: frozenset[str],
                   namespaces: tuple[str, ...] = ROBINHOOD_NAMESPACES) -> frozenset[str]:
    """The closed receipt/audit set of fully-qualified read tools for these basenames."""
    return frozenset(name for basename in basenames for name in qualified(basename, namespaces))


def live_tools(basenames: frozenset[str]) -> frozenset[str]:
    """The closed set permitted in a new live invocation (legacy namespace excluded)."""
    return approved_tools(basenames, ROBINHOOD_LIVE_NAMESPACES)


def is_approved(tool_name: str, basenames: frozenset[str],
                namespaces: tuple[str, ...] = ROBINHOOD_NAMESPACES) -> bool:
    """True only when namespace AND basename both match an approved pair."""
    namespace, basename = split_tool(tool_name)
    if namespace is None or basename is None:
        return False
    return namespace in namespaces and basename in basenames


def is_live_approved(tool_name: str, basenames: frozenset[str]) -> bool:
    """True only for the current live namespace plus an approved read basename."""
    return is_approved(tool_name, basenames, ROBINHOOD_LIVE_NAMESPACES)


def namespace_status(tool_name: str,
                     namespaces: tuple[str, ...] = ROBINHOOD_NAMESPACES) -> str:
    """Classify a tool's namespace so a caller can emit a precise receipt.

    Returns one of: 'live', 'legacy', 'unknown_namespace', 'malformed'. A dead or unknown
    namespace must surface as an explicit status — never as silently empty data.

    Membership tests, not identity comparisons: RETIRED is derived by SUBTRACTING the live
    tuple from the archive tuple, so 'live' and 'legacy' are disjoint by construction. A future
    rename that makes the two constants equal can therefore never relabel archived receipts as
    live (which would silently disarm validate_robinhood_snapshot's
    `collector_audit_legacy_namespace_not_live` gate).
    """
    namespace, _basename = split_tool(tool_name)
    if namespace is None:
        return "malformed"
    if namespace in ROBINHOOD_LIVE_NAMESPACES:
        return "live"
    if namespace in ROBINHOOD_RETIRED_NAMESPACES and namespace in namespaces:
        return "legacy"
    return "unknown_namespace"


# --- One-directional live-presence assertion --------------------------------
# Fail-closed classification of a MEASURED registry observation. Pure function: the caller
# (scripts/mcp_registry_probe.py) does the measuring; this module owns the verdict so the
# security contract stays in exactly one place.
#
# Direction matters. This asks "is the namespace I already declared actually mounted and
# connected?" It never asks "which namespace should I adopt?" — see the SECURITY CONTRACT note
# about the rejected permissive resolver.
LIVE_PRESENCE_OK = "live_connected"
CONNECTED_STATUS = "connected"
NEEDS_AUTH_STATUS = "needs_authentication"

# The exact reviewed read surface the live namespace MUST expose. "Connected" is not enough:
# on 2026-08-04 the directly configured server completed OAuth twice, reported status=connected
# with no mcp_server_errors, and advertised ZERO tools. A server that answers the handshake but
# serves nothing is indistinguishable, downstream, from a dead namespace — so the inventory is
# now part of the gate, not an afterthought.
REQUIRED_LIVE_READ_BASENAMES: frozenset[str] = frozenset({
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_orders",
    "get_equity_quotes",
    "get_equity_tradability",
    "get_realized_pnl",
})


def classify_live_inventory(observed_tools) -> tuple[str, dict]:
    """Fail-closed check that the LIVE namespace exposes exactly the reviewed read surface.

    `observed_tools` is a MEASURED list of fully-qualified tool names (from the structured
    `system/init` inventory — never model prose). Returns (classification, evidence):

      inventory_ok                 - every required read basename present under the live namespace
      inventory_empty              - the live namespace advertises no tools at all
      inventory_incomplete         - present but missing one or more required read basenames
      inventory_unobserved         - no inventory was supplied (cannot be proven; fails closed)

    Extra tools under the live namespace are REPORTED but never fatal here: authority still comes
    from the explicit allowlist, and a broker adding a new read must not silently block research.
    A mutation-shaped extra is surfaced so the deny surface can be re-audited.
    """
    evidence = {
        "declared_live_namespace": ROBINHOOD_LIVE_NAMESPACE,
        "required_read_basenames": sorted(REQUIRED_LIVE_READ_BASENAMES),
        "observed_live_tool_count": 0,
        "observed_read_basenames": [],
        "missing_read_basenames": sorted(REQUIRED_LIVE_READ_BASENAMES),
        "extra_live_basenames": [],
    }
    if observed_tools is None:
        return "inventory_unobserved", evidence

    live: set[str] = set()
    for name in observed_tools:
        namespace, basename = split_tool(name)
        if namespace in ROBINHOOD_LIVE_NAMESPACES and basename:
            live.add(basename)
    evidence["observed_live_tool_count"] = len(live)
    evidence["observed_read_basenames"] = sorted(live & REQUIRED_LIVE_READ_BASENAMES)
    missing = sorted(REQUIRED_LIVE_READ_BASENAMES - live)
    evidence["missing_read_basenames"] = missing
    evidence["extra_live_basenames"] = sorted(live - REQUIRED_LIVE_READ_BASENAMES)
    if not live:
        return "inventory_empty", evidence
    if missing:
        return "inventory_incomplete", evidence
    return "inventory_ok", evidence


def classify_live_presence(observations) -> tuple[str, dict]:
    """Classify the declared live Robinhood namespace against a measured registry.

    `observations` is an iterable of (namespace, status) pairs, one per server the probe saw.
    `status` is a normalized token, typically 'connected' | 'needs_authentication' | 'failed'.

    Returns (classification, evidence). Classification is exactly one of:
      live_connected                 - exactly one match, connected. The ONLY passing verdict.
      registry_unavailable           - nothing observable; the probe could not measure.
      namespace_missing              - the declared live namespace is not in the registry.
      namespace_not_connected        - present but not connected.
      namespace_needs_authentication - present but unauthenticated.
      namespace_duplicate            - the declared namespace appears more than once.
      retired_namespace_present      - a RETIRED Robinhood namespace is also mounted, so the
                                       live identity is ambiguous.
    Every non-OK verdict is a fail-closed condition for the caller.
    """
    pairs: list[tuple[str, str]] = []
    for item in (observations or ()):
        try:
            namespace, status = item
        except (TypeError, ValueError):
            continue
        if isinstance(namespace, str) and namespace:
            pairs.append((namespace, str(status or "").strip().lower()))

    evidence = {
        "declared_live_namespace": ROBINHOOD_LIVE_NAMESPACE,
        "observed_server_count": len(pairs),
        "live_match_count": 0,
        "live_statuses": [],
        "retired_namespaces_present": [],
    }
    if not pairs:
        return "registry_unavailable", evidence

    live_statuses = [s for ns, s in pairs if ns == ROBINHOOD_LIVE_NAMESPACE]
    retired_present = sorted({ns for ns, _ in pairs if ns in ROBINHOOD_RETIRED_NAMESPACES})
    evidence["live_match_count"] = len(live_statuses)
    evidence["live_statuses"] = sorted(set(live_statuses))
    evidence["retired_namespaces_present"] = retired_present

    if retired_present:
        return "retired_namespace_present", evidence
    if not live_statuses:
        return "namespace_missing", evidence
    if len(live_statuses) > 1:
        return "namespace_duplicate", evidence
    status = live_statuses[0]
    if status == NEEDS_AUTH_STATUS:
        return "namespace_needs_authentication", evidence
    if status != CONNECTED_STATUS:
        return "namespace_not_connected", evidence
    return LIVE_PRESENCE_OK, evidence


HYDRATOR_APPROVED_TOOLS: frozenset[str] = approved_tools(HYDRATOR_READ_BASENAMES)
COLLECTOR_APPROVED_TOOLS: frozenset[str] = approved_tools(COLLECTOR_READ_BASENAMES)
HYDRATOR_LIVE_TOOLS: frozenset[str] = live_tools(HYDRATOR_READ_BASENAMES)
COLLECTOR_LIVE_TOOLS: frozenset[str] = live_tools(COLLECTOR_READ_BASENAMES)
