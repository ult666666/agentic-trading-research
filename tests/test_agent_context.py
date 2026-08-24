#!/usr/bin/env python3
"""Adversarial tests for the bounded agent-context compiler."""
from __future__ import annotations

import ast
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

import agent_context as context

ROOT = Path(__file__).resolve().parent.parent


class TestAgentContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(context.POLICY_PATH.read_text(encoding="utf-8"))
        cls.index = context.build_index()

    def test_policy_and_output_schemas_validate(self):
        schemas = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / "schemas").glob("agent_context_*.schema.json"))
        ]
        for schema in schemas:
            jsonschema.Draft202012Validator.check_schema(schema)
        registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in schemas])
        by_title = {schema["title"]: schema for schema in schemas}
        jsonschema.Draft202012Validator(
            by_title["Agent Context Policy"], registry=registry).validate(self.policy)
        jsonschema.Draft202012Validator(
            by_title["Agent Context Index"], registry=registry).validate(self.index)
        component = context.build_component("model.risk_gate")
        jsonschema.Draft202012Validator(
            by_title["Agent Context Component"], registry=registry).validate(component)

    def test_inventory_is_complete_unique_and_domain_routed(self):
        self.assertEqual(context.validate_index(self.index, policy=self.policy), [])
        counts = self.index["counts"]
        self.assertEqual(counts, {
            "domains": 7, "components": 47, "anchors": 8, "workflows": 5, "models": 34,
        })
        ids = [row["id"] for row in self.index["components"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            sum(row["component_count"] for row in self.index["domains"]),
            counts["components"])
        self.assertTrue(all(row["authority"] == "research_only_no_execution"
                            for row in self.index["components"]))

    def test_progressive_disclosure_is_bounded_and_materially_smaller(self):
        budgets = self.index["budgets"]
        self.assertLessEqual(budgets["brief_characters"], budgets["brief_max_characters"])
        self.assertLessEqual(budgets["index_characters"], budgets["index_max_characters"])
        self.assertLess(budgets["brief_approximate_tokens"],
                        budgets["index_approximate_tokens"] // 5)
        component = context.build_component("model.risk_gate")
        self.assertEqual(context.validate_component(component), [])
        self.assertLess(component["budget"]["approximate_tokens"],
                        budgets["index_approximate_tokens"] // 3)

    def test_compilation_is_deterministic_and_source_hash_bound(self):
        rebuilt = context.build_index()
        self.assertEqual(rebuilt, self.index)
        expected = {"policy", "constitution", "model_inventory", "workflows"}
        self.assertEqual(set(rebuilt["source_hashes"]), expected)
        self.assertTrue(all(len(value) == 64 for value in rebuilt["source_hashes"].values()))

    def test_component_detail_is_one_component_only_and_keeps_non_authority(self):
        risk = context.build_component("model.risk_gate")
        self.assertEqual(risk["component"]["id"], "model.risk_gate")
        self.assertIn("limitations", risk["detail"])
        self.assertIn("Never authorizes execution", risk["detail"]["prohibited_use"])
        workflow = context.build_component("workflow.post_open")
        self.assertEqual(workflow["detail"]["dependencies"], ["morning"])
        self.assertEqual(workflow["detail"]["scheduled_pt"], "06:45")
        self.assertFalse(workflow["safety"]["execution_enabled"])

    def test_unknown_component_and_incomplete_model_mapping_fail_closed(self):
        with self.assertRaisesRegex(context.ContextError, "component_not_found"):
            context.build_component("model.does_not_exist")
        changed = copy.deepcopy(self.policy)
        changed["model_domains"].pop("risk_gate")
        inventory = json.loads((ROOT / "data" / "model_inventory.json").read_text())
        self.assertIn("policy_model_inventory_mapping_incomplete",
                      context.validate_policy(changed, inventory=inventory))

    def test_tampering_private_shapes_and_authority_lies_are_rejected(self):
        changed = copy.deepcopy(self.index)
        changed["components"][0]["summary"] = "owner@example.invalid"
        changed["artifact_sha256"] = context.sha256_obj({
            key: value for key, value in changed.items() if key != "artifact_sha256"})
        changed["budgets"]["index_characters"] = len(context.canonical(changed))
        changed["artifact_sha256"] = context.sha256_obj({
            key: value for key, value in changed.items() if key != "artifact_sha256"})
        self.assertIn("index_email_present", context.validate_index(changed))
        changed = copy.deepcopy(self.index)
        changed["components"][0]["authority"] = "execution"
        changed["artifact_sha256"] = context.sha256_obj({
            key: value for key, value in changed.items() if key != "artifact_sha256"})
        self.assertIn("index_component_authority_or_domain_invalid",
                      context.validate_index(changed))

    def test_index_excludes_runtime_reports_prompts_prices_accounts_and_credentials(self):
        encoded = context.canonical(self.index).lower()
        for forbidden in (
                "reports/", "prompts/", "runtime/", "account_number", "buying_power",
                "portfolio.json", "secrets/", "api_key", "access_token"):
            self.assertNotIn(forbidden, encoded)
        safety = self.index["safety"]
        for key in (
                "execution_enabled", "activation_authority", "strategy_change_allowed",
                "profitability_claim_allowed", "account_data_allowed", "market_price_data_allowed",
                "report_content_allowed", "credentials_allowed", "network_invoked",
                "model_invoked", "broker_invoked"):
            self.assertFalse(safety[key])

    def test_cli_levels_are_valid_and_do_not_write_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            before = list(Path(temporary).iterdir())
            output = io.StringIO()
            with redirect_stdout(output):
                code = context.main(["brief", "--format", "json"])
            self.assertEqual(code, 0)
            brief = json.loads(output.getvalue())
            self.assertEqual(brief["schema"], context.BRIEF_SCHEMA)
            self.assertLessEqual(brief["budget"]["characters"], brief["budget"]["max_characters"])
            self.assertEqual(list(Path(temporary).iterdir()), before)

    def test_compiler_has_no_network_model_broker_or_process_surface(self):
        source_path = ROOT / "scripts" / "agent_context.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    calls.add(target.id)
                elif isinstance(target, ast.Attribute):
                    calls.add(target.attr)
        self.assertTrue(imports.isdisjoint({
            "socket", "subprocess", "requests", "urllib", "httpx", "anthropic", "openai"}))
        self.assertTrue(calls.isdisjoint({
            "run", "Popen", "urlopen", "request", "place_order", "preview_order"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
