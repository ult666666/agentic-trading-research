#!/usr/bin/env python3
"""Tests for deterministic catalyst/event graph construction and safety."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import catalyst_graph as cg  # noqa: E402
import test_evidence_schema as tes  # noqa: E402

T = tes.T
TAXONOMY = ROOT / "config" / "catalyst_taxonomy.json"


class CatalystGraphTests(unittest.TestCase):
    def _fixture(self, root: Path, mutate=None) -> tuple[Path, dict]:
        rdir = root / "data" / "runtime" / "research" / T
        rdir.mkdir(parents=True, exist_ok=True)
        evidence = tes.evidence(rdir)
        if mutate:
            mutate(evidence)
        path = rdir / "evidence.json"
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True))
        return path, evidence

    def _build(self, root: Path, mutate=None):
        evidence_path, evidence = self._fixture(root, mutate)
        result = cg.build_for_target(
            T, root=root, evidence_path=evidence_path, taxonomy_path=TAXONOMY)
        graph = json.loads(Path(result["path"]).read_text())
        return result, graph, evidence_path, evidence

    def test_valid_graph_is_hash_bound_and_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result, graph, evidence_path, _ = self._build(root)
            self.assertTrue(result["ok"])
            self.assertEqual(graph["build_status"], "diagnostic_only_unverified")
            self.assertFalse(graph["input_evidence_valid"])
            self.assertTrue(graph["input_evidence_structurally_usable_for_diagnostics"])
            self.assertEqual(graph["summary"]["event_count"], 1)
            self.assertEqual(graph["summary"]["verdict_counts"]["confirmed"], 0)
            self.assertEqual(graph["summary"]["verdict_counts"]["unverified"], 1)
            self.assertEqual(graph["event_verdicts"][0]["linked_claim_ids"], ["c1", "s1"])
            self.assertFalse(graph["event_verdicts"][0]["grants_actionability"])
            self.assertFalse(any(edge["relation"] == "confirms" for edge in graph["edges"]))
            sources = [node for node in graph["nodes"] if node["type"] == "source_document"]
            self.assertTrue(all(node["catalyst_authority_status"] == "lead_only"
                                for node in sources))
            self.assertEqual(graph["safety"], cg.SAFETY)
            self.assertEqual(cg.validate_graph(
                graph, root=root, evidence_path=evidence_path, taxonomy_path=TAXONOMY), [])

    def test_explicit_claim_links_prevent_cross_event_borrowing(self):
        def mutate(ev):
            ev["claims"].append(tes.secondary_claim(claim_id="s2", symbol="SPY",
                                                      corroboration_group="g3",
                                                      source_domain="bloomberg.com"))
            ev["catalysts"].append(tes.catalyst(
                event="separate same-symbol event", catalyst_type="guidance",
                claim_ids=["s2"], primary_source_found=True,
                independent_corroboration_count=1))

        with tempfile.TemporaryDirectory() as td:
            _, graph, _, _ = self._build(Path(td), mutate)
            verdicts = {next(n["untrusted_data_label"] for n in graph["nodes"]
                             if n["id"] == v["event_id"]): v
                        for v in graph["event_verdicts"]}
            self.assertEqual(verdicts["Q2 earnings"]["verdict"], "unverified")
            self.assertEqual(verdicts["separate same-symbol event"]["verdict"], "unverified")
            self.assertFalse(verdicts["separate same-symbol event"]["effective_primary"])
            self.assertEqual(verdicts["separate same-symbol event"]["linked_claim_ids"], ["s2"])

    def test_syndicated_sources_are_linked_to_one_origin(self):
        def mutate(ev):
            ev["claims"].append(tes.secondary_claim(
                claim_id="s2", symbol="SPY", corroboration_group="g2",
                source_domain="news-copy.example"))
            ev["catalysts"][0]["claim_ids"].append("s2")
            ev["catalysts"][0]["independent_corroboration_count"] = 2

        with tempfile.TemporaryDirectory() as td:
            _, graph, _, _ = self._build(Path(td), mutate)
            synd = [e for e in graph["edges"] if e["relation"] == "syndicated_copy"]
            self.assertEqual(len(synd), 1)
            self.assertEqual(synd[0]["corroboration_group"], "g2")
            self.assertEqual(graph["event_verdicts"][0]["effective_independent_sources"], 0)

    def test_unresolved_linked_conflict_is_preserved(self):
        def mutate(ev):
            ev["claims"][0]["validation_status"] = "conflicted"
            ev["claims"][0]["conflicts_with"] = ["s1"]
            ev["claims"][1]["validation_status"] = "conflicted"
            ev["claims"][1]["conflicts_with"] = ["c1"]

        with tempfile.TemporaryDirectory() as td:
            _, graph, _, _ = self._build(Path(td), mutate)
            verdict = graph["event_verdicts"][0]
            self.assertEqual(verdict["verdict"], "conflicted")
            self.assertTrue(verdict["flags"]["unresolved_conflict"])
            self.assertTrue(any(e["relation"] == "contradicts" and e.get("conflict_preserved")
                                for e in graph["edges"]))

    def test_unknown_type_maps_to_other_without_inventing_flags(self):
        def mutate(ev):
            ev["catalysts"][0]["catalyst_type"] = "novel_unregistered_event"

        with tempfile.TemporaryDirectory() as td:
            _, graph, _, _ = self._build(Path(td), mutate)
            event = next(n for n in graph["nodes"] if n["type"] == "event")
            verdict = graph["event_verdicts"][0]
            self.assertEqual(event["event_category"], "other")
            self.assertTrue(event["unknown_type_mapped_to_other"])
            self.assertFalse(verdict["flags"]["binary_event"])
            self.assertFalse(verdict["flags"]["dilution_financing"])

    def test_invalid_evidence_writes_blocked_empty_graph(self):
        def mutate(ev):
            ev["catalysts"][0].pop("symbol")

        with tempfile.TemporaryDirectory() as td:
            result, graph, _, _ = self._build(Path(td), mutate)
            self.assertFalse(result["ok"])
            self.assertEqual(graph["build_status"], "blocked")
            self.assertEqual(graph["nodes"], [])
            self.assertTrue(any("missing_key:symbol" in x for x in graph["blockers"]))
            self.assertFalse(graph["safety"]["execution_enabled"])

    def test_idempotent_rebuild_and_immutable_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result, _, evidence_path, _ = self._build(root)
            again = cg.build_for_target(
                T, root=root, evidence_path=evidence_path, taxonomy_path=TAXONOMY)
            self.assertEqual(again["action"], "idempotent_existing")
            evidence = json.loads(evidence_path.read_text())
            evidence["catalysts"][0]["event"] = "changed after graph publication"
            evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "immutable_catalyst_graph_conflict"):
                cg.build_for_target(
                    T, root=root, evidence_path=evidence_path, taxonomy_path=TAXONOMY,
                    out=Path(result["path"]))

    def test_tamper_and_taxonomy_drift_fail_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, graph, evidence_path, _ = self._build(root)
            graph["summary"]["event_count"] = 99
            errors = cg.validate_graph(
                graph, root=root, evidence_path=evidence_path, taxonomy_path=TAXONOMY)
            self.assertIn("graph_self_hash_mismatch", errors)
            errors = cg.validate_graph(
                json.loads((root / "data/runtime/research" / T / "catalyst_graph.json").read_text()),
                root=root, evidence_path=evidence_path, taxonomy_path=root / "missing-taxonomy.json")
            self.assertIn("taxonomy_hash_mismatch", errors)

    def test_no_execution_model_or_network_surface(self):
        source = (ROOT / "scripts" / "catalyst_graph.py").read_text().lower()
        for token in ("place_equity_order", "review_equity_order", "cancel_equity_order",
                      "submit_order", "preview_order", "detect_claude", "urlopen",
                      "requests.", "websearch", "webfetch"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
