#!/usr/bin/env python3
"""Regression tests for authoritative brokerage namespace identity.

Covers the 2026-07-22 connector-rename class of defect and the hostile-namespace attacks the
mapping must reject. Pure identity logic: no network, no broker, no file writes.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import broker_namespace_policy as bnp  # noqa: E402
from public_snapshot import requires_files  # noqa: E402

LIVE = bnp.ROBINHOOD_LIVE_NAMESPACE
LEGACY = bnp.ROBINHOOD_LEGACY_NAMESPACE


class TestNamespaceIdentity(unittest.TestCase):
    def test_current_approved_uuid_namespace_is_accepted(self):
        self.assertTrue(bnp.is_approved(f"mcp__{LIVE}__get_equity_quotes",
                                        bnp.HYDRATOR_READ_BASENAMES))
        self.assertEqual(bnp.namespace_status(f"mcp__{LIVE}__get_equity_quotes"), "live")

    def test_legacy_namespace_is_still_classifiable_for_historical_evidence(self):
        self.assertTrue(bnp.is_approved(f"mcp__{LEGACY}__get_equity_quotes",
                                        bnp.HYDRATOR_READ_BASENAMES))
        self.assertEqual(bnp.namespace_status(f"mcp__{LEGACY}__get_equity_quotes"), "legacy")

    def test_unknown_uuid_namespace_fails_closed(self):
        hostile = "mcp__00000000-dead-beef-0000-000000000000__get_equity_quotes"
        self.assertFalse(bnp.is_approved(hostile, bnp.HYDRATOR_READ_BASENAMES))
        self.assertEqual(bnp.namespace_status(hostile), "unknown_namespace")

    def test_same_basename_under_hostile_namespace_is_rejected(self):
        """Basename alone must never authorize — this is the core attack."""
        for namespace in ("evil", "claude_ai_Webull", "kalshi", "github"):
            tool = f"mcp__{namespace}__get_equity_quotes"
            self.assertFalse(bnp.is_approved(tool, bnp.HYDRATOR_READ_BASENAMES), tool)
            self.assertEqual(bnp.namespace_status(tool), "unknown_namespace", tool)

    def test_approved_read_tool_in_approved_namespace(self):
        for basename in bnp.HYDRATOR_READ_BASENAMES:
            self.assertTrue(bnp.is_approved(f"mcp__{LIVE}__{basename}",
                                            bnp.HYDRATOR_READ_BASENAMES))

    def test_unapproved_read_tool_is_rejected_even_in_approved_namespace(self):
        """A real read tool the hydrator lane was never granted stays out."""
        for basename in ("get_option_chains", "get_equity_historicals", "get_financials",
                         "get_accounts", "get_portfolio"):
            self.assertFalse(bnp.is_approved(f"mcp__{LIVE}__{basename}",
                                             bnp.HYDRATOR_READ_BASENAMES), basename)

    def test_order_and_mutation_tools_are_never_approved(self):
        for basename in ("place_equity_order", "cancel_equity_order", "place_option_order",
                         "exercise_option", "cancel_option_exercise", "create_watchlist"):
            for namespace in (LIVE, LEGACY):
                tool = f"mcp__{namespace}__{basename}"
                self.assertFalse(bnp.is_approved(tool, bnp.HYDRATOR_READ_BASENAMES), tool)
                self.assertFalse(bnp.is_approved(tool, bnp.COLLECTOR_READ_BASENAMES), tool)

    def test_empty_or_malformed_tool_configuration_fails_closed(self):
        for value in ("", "get_equity_quotes", "mcp__onlyone", "mcp____", None, 42,
                      "mcp__ns__base__extra"):
            self.assertFalse(bnp.is_approved(value, bnp.HYDRATOR_READ_BASENAMES), repr(value))
        self.assertEqual(bnp.split_tool("not a tool"), (None, None))
        self.assertEqual(bnp.namespace_status("not a tool"), "malformed")

    def test_hydrator_scope_is_a_strict_subset_of_collector_scope(self):
        self.assertTrue(bnp.HYDRATOR_READ_BASENAMES < bnp.COLLECTOR_READ_BASENAMES)
        self.assertNotIn("get_portfolio", bnp.HYDRATOR_READ_BASENAMES)
        self.assertNotIn("get_accounts", bnp.HYDRATOR_READ_BASENAMES)

    def test_approved_sets_cover_every_archive_namespace_exactly(self):
        """2026-08-04: the archive tuple is live + BOTH retired identities (three), because the
        connector name and the UUID alias must each stay classifiable for archived receipts."""
        span = len(bnp.ROBINHOOD_ARCHIVE_NAMESPACES)
        self.assertEqual(span, 3)
        self.assertEqual(len(bnp.HYDRATOR_APPROVED_TOOLS), span * len(bnp.HYDRATOR_READ_BASENAMES))
        self.assertEqual(len(bnp.COLLECTOR_APPROVED_TOOLS), span * len(bnp.COLLECTOR_READ_BASENAMES))
        for tool in bnp.COLLECTOR_APPROVED_TOOLS:
            namespace, _ = bnp.split_tool(tool)
            self.assertIn(namespace, bnp.ROBINHOOD_NAMESPACES)

    def test_live_sets_exclude_legacy_namespace(self):
        self.assertEqual(len(bnp.HYDRATOR_LIVE_TOOLS), len(bnp.HYDRATOR_READ_BASENAMES))
        self.assertEqual(len(bnp.COLLECTOR_LIVE_TOOLS), len(bnp.COLLECTOR_READ_BASENAMES))
        for tool in bnp.COLLECTOR_LIVE_TOOLS:
            namespace, _ = bnp.split_tool(tool)
            self.assertEqual(namespace, LIVE)
            self.assertTrue(bnp.is_live_approved(tool, bnp.COLLECTOR_READ_BASENAMES))
        self.assertFalse(bnp.is_live_approved(
            f"mcp__{LEGACY}__get_equity_quotes", bnp.COLLECTOR_READ_BASENAMES))

    @requires_files("scripts/validate_broker_permissions.py")
    def test_every_live_tool_is_also_in_the_permission_validator_allowlist(self):
        """The one-authoritative-source guarantee: no consumer may diverge.

        2026-08-04: scoped to the LIVE set. A retired namespace must appear in NO positive
        allow path, so pinning the two-namespace APPROVED set against READ_ALLOWLIST would now
        pin the very drift the operator decision removed.
        """
        import validate_broker_permissions as vbp
        missing = sorted(bnp.COLLECTOR_LIVE_TOOLS - set(vbp.READ_ALLOWLIST))
        self.assertEqual(missing, [], f"not in READ_ALLOWLIST: {missing}")

    @requires_files("scripts/validate_broker_permissions.py")
    def test_retired_namespace_reads_are_in_no_positive_allow_path(self):
        """Deny-only: the retired namespace may never re-enter a read allowlist."""
        import validate_broker_permissions as vbp
        for namespace in bnp.ROBINHOOD_RETIRED_NAMESPACES:
            for basename in bnp.COLLECTOR_READ_BASENAMES:
                tool = f"mcp__{namespace}__{basename}"
                self.assertNotIn(tool, vbp.READ_ALLOWLIST, tool)
                self.assertFalse(bnp.is_live_approved(tool, bnp.COLLECTOR_READ_BASENAMES), tool)

    @requires_files("scripts/validate_broker_permissions.py", ".claude/settings.json")
    def test_retired_namespace_mutations_remain_denied(self):
        """The ONLY place a retired namespace survives is the mutation deny list."""
        import validate_broker_permissions as vbp
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        deny = set(settings["permissions"]["deny"])
        # Every retired identity must be wholly denied — one `mcp__<server>` entry each — and the
        # UUID alias additionally keeps its exact-name mutation denies (LEGACY_HASH_DENIES).
        for namespace in bnp.ROBINHOOD_RETIRED_NAMESPACES:
            self.assertIn(f"mcp__{namespace}", deny,
                          f"retired namespace not wholly denied: {namespace}")
        retained = [name for name in vbp.LEGACY_HASH_DENIES
                    if name.startswith(f"mcp__{bnp.ROBINHOOD_UUID_ALIAS_NAMESPACE}__")]
        self.assertTrue(retained, "retired-namespace exact denies must still be pinned")
        for name in retained:
            self.assertIn(name, deny, f"retired-namespace deny lost: {name}")

    @requires_files("scripts/candidate_quote_hydrator.py")
    def test_hydrator_uses_the_shared_authority_not_its_own_literals(self):
        """A rename must not require editing an unrelated constant in the hydrator."""
        import candidate_quote_hydrator as cqh
        self.assertEqual(set(cqh.EXPECTED_BROKER_TOOLS), set(bnp.HYDRATOR_APPROVED_TOOLS))
        source = (ROOT / "scripts" / "candidate_quote_hydrator.py").read_text()
        self.assertNotIn(f'"mcp__{LEGACY}__get_equity_quotes"', source,
                         "hydrator still hardcodes a namespace literal")

    @requires_files("scripts/run_robinhood_readonly_collector.sh",
                    "prompts/robinhood_readonly_collector.md")
    def test_full_collector_live_surface_renders_from_shared_policy(self):
        runner = (ROOT / "scripts" / "run_robinhood_readonly_collector.sh").read_text()
        prompt = (ROOT / "prompts" / "robinhood_readonly_collector.md").read_text()
        self.assertIn("policy.COLLECTOR_LIVE_TOOLS", runner)
        self.assertIn("policy.live_qualified(basename)", runner)
        self.assertNotIn(f"mcp__{LEGACY}__", runner)
        self.assertNotIn(f"mcp__{LEGACY}__", prompt)
        self.assertIn("{{RH_GET_EQUITY_QUOTES_TOOL}}", prompt)
        self.assertIn("--audit-receipt", runner)

    @requires_files("scripts/candidate_quote_hydrator.py",
                    "scripts/run_candidate_quote_hydrator.sh",
                    "prompts/robinhood_candidate_quote_collector.md",
                    "config/candidate_quote_hydration.json")
    def test_candidate_collector_live_surface_renders_from_shared_policy(self):
        import json
        import candidate_quote_hydrator as cqh

        runner = (ROOT / "scripts" / "run_candidate_quote_hydrator.sh").read_text()
        prompt = (ROOT / "prompts" / "robinhood_candidate_quote_collector.md").read_text()
        config = json.loads(
            (ROOT / "config" / "candidate_quote_hydration.json").read_text())
        # 2026-08-04: the config is a POSITIVE allow path and now carries EXACTLY the live set.
        # It previously carried both namespaces "as a hash anchor", which let it advertise a
        # dead identity indefinitely while the runner rendered something else — the same drift
        # that killed the collector. The runner asserts equality too (fail-closed, pre-model).
        self.assertEqual(
            set(config["collector"]["allowed_read_tools"]),
            set(bnp.HYDRATOR_LIVE_TOOLS))
        self.assertEqual(set(cqh.LIVE_BROKER_TOOLS), set(bnp.HYDRATOR_LIVE_TOOLS))
        self.assertEqual(set(cqh.EXPECTED_BROKER_TOOLS),
                         set(bnp.HYDRATOR_APPROVED_TOOLS))
        self.assertIn("namespace_policy.HYDRATOR_LIVE_TOOLS", runner)
        self.assertIn("namespace_policy.live_qualified", runner)
        self.assertNotIn(f"mcp__{LEGACY}__", runner)
        self.assertNotIn(f"mcp__{LEGACY}__", prompt)
        self.assertIn("{{RH_GET_EQUITY_QUOTES_TOOL}}", prompt)
        self.assertIn("{{RH_GET_EQUITY_TRADABILITY_TOOL}}", prompt)


@requires_files("data/broker_tool_namespace.json", "scripts/validate_broker_permissions.py")
class TestNamespaceSnapshotCannotSilentlyMisauthorize(unittest.TestCase):
    """`data/broker_tool_namespace.json` is a committed production BOOTSTRAP REGISTRY.

    Classification evidence (not filename guessing): it is loaded at
    broker_runtime_audit.py:326 into RECOGNIZED_SERVERS and consulted by classify_tool at :373,
    it is validate_broker_permissions.DEFAULT_SNAPSHOT, and it is pinned by build_manifest
    CRITICAL, control_plane_integrity, and documentation_truth. It therefore carries authority
    and is neither an inert fixture nor deprecated evidence.

    Its `captured_pt` is 2026-07-08 — the date that tool-category AUDIT was performed, which is
    deliberately not re-stamped without a re-audit. These tests pin the property that actually
    matters: the snapshot must not silently authorize or block the wrong namespace, whether it
    happens to be current or lagging.
    """

    def setUp(self):
        import json as _json
        self.snapshot = _json.loads(
            (ROOT / "data" / "broker_tool_namespace.json").read_text())

    def test_live_namespace_is_recognized(self):
        self.assertIn(LIVE, self.snapshot["recognized_broker_servers"])

    def test_retired_namespace_stays_recognized_so_its_denies_bind(self):
        """A deny entry for a namespace the snapshot does not recognize protects nothing."""
        for namespace in bnp.ROBINHOOD_RETIRED_NAMESPACES:
            self.assertIn(namespace, self.snapshot["recognized_broker_servers"], namespace)

    def test_authority_comes_from_the_allowlist_not_the_inventory(self):
        """The `servers` inventory is descriptive; authority comes from READ_ALLOWLIST.

        2026-08-04: the live namespace is once again present in the descriptive inventory, so
        this asserts the invariant directly instead of via the incidental fact that it was
        absent — presence must not be what grants, and absence must not be what blocks.
        """
        import broker_runtime_audit as bra
        import validate_broker_permissions as vbp
        tool = f"mcp__{LIVE}__get_equity_quotes"
        self.assertIn(tool, vbp.READ_ALLOWLIST)                   # authority is explicit
        self.assertEqual(bra.classify_tool(tool), "broker_read_only")
        # A basename present in the inventory but absent from READ_ALLOWLIST stays unknown.
        self.assertEqual(bra.classify_tool(f"mcp__{LIVE}__get_option_chains_not_allowlisted"),
                         "unknown_broker_tool")

    def test_retired_namespace_read_is_not_a_recognized_broker_read(self):
        """Removing it from every positive path is what makes it deny-only."""
        import broker_runtime_audit as bra
        for namespace in bnp.ROBINHOOD_RETIRED_NAMESPACES:
            self.assertNotEqual(
                bra.classify_tool(f"mcp__{namespace}__get_equity_quotes"), "broker_read_only")

    def test_unrecognized_server_still_fails_closed(self):
        import broker_runtime_audit as bra
        hostile = "mcp__11111111-2222-3333-4444-555555555555__get_equity_quotes"
        self.assertNotIn("11111111-2222-3333-4444-555555555555",
                         self.snapshot["recognized_broker_servers"])
        self.assertEqual(bra.classify_tool(hostile), "unknown_broker_tool")

    def test_snapshot_presence_alone_never_authorizes_a_mutation_tool(self):
        import broker_runtime_audit as bra
        for server in self.snapshot["recognized_broker_servers"]:
            self.assertNotEqual(
                bra.classify_tool(f"mcp__{server}__place_equity_order"), "broker_read_only")


if __name__ == "__main__":
    unittest.main()
