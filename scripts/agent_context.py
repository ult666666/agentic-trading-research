#!/usr/bin/env python3
"""Deterministic, bounded context compiler for agents and the operator console.

The compiler provides progressive disclosure over authoritative local contracts:

* ``brief``: the smallest useful system orientation;
* ``index``: domains plus bounded component summaries;
* ``show``: one component's bounded contract detail.

It never reads reports, runtime market/account data, prompts, secrets, or broker payloads.
It performs no network, broker, model, or subprocess call and grants no authority.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import model_inventory  # noqa: E402

POLICY_SCHEMA = "agent_context_policy/1.0"
INDEX_SCHEMA = "agent_context_index/1.0"
COMPONENT_SCHEMA = "agent_context_component/1.0"
BRIEF_SCHEMA = "agent_context_brief/1.0"
POLICY_PATH = ROOT / "config" / "agent_context.json"

SOURCE_KEYS = ("constitution", "model_inventory", "workflows")
EXPECTED_SOURCE_PATHS = {
    "constitution": "AGENTS.md",
    "model_inventory": "data/model_inventory.json",
    "workflows": "config/autonomy_workflows.json",
}
SAFETY = {
    "research_only": True,
    "read_only": True,
    "local_artifact_write_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "profitability_claim_allowed": False,
    "account_data_allowed": False,
    "market_price_data_allowed": False,
    "report_content_allowed": False,
    "credentials_allowed": False,
    "network_invoked": False,
    "model_invoked": False,
    "broker_invoked": False,
}
ALLOWED_POLICY_KEYS = {
    "schema", "version", "index_schema", "component_schema", "source_paths",
    "budgets", "system", "domains", "anchors", "model_domains", "safety",
}
TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COMPONENT_RE = re.compile(r"^(anchor|workflow|model)\.[a-z][a-z0-9_]{0,79}$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MASKED_ACCOUNT_RE = re.compile(r"(?:\*{4}|\u2022{4})\d{4}")
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:(?<![a-z0-9])sk-[a-z0-9_-]{8,}|(?:access|refresh)[_-]?token\s*[:=]\s*[^\s,}]{6,}|"
    r"password\s*[:=]\s*[^\s,}]{4,})")


class ContextError(RuntimeError):
    """Safe context-compiler failure."""


def canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_obj(document: Any) -> str:
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ContextError("source_path_invalid")
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ContextError("source_path_invalid")
    path = root / rel
    if path.is_symlink() or not path.is_file():
        raise ContextError(f"source_missing_or_symlink:{relative}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ContextError(f"source_outside_root:{relative}") from exc
    return path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContextError(f"json_unreadable:{path.name}") from exc
    if not isinstance(value, dict):
        raise ContextError(f"json_not_object:{path.name}")
    return value


def _bounded_text(value: Any, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= maximum:
        return text
    return text[: max(1, maximum - 3)].rstrip() + "..."


def _first_sentence(value: Any, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    parts = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    return _bounded_text(parts[0], maximum)


def _label(value: str) -> str:
    return " ".join(word.capitalize() for word in value.split("_"))


def _approx_tokens(characters: int, policy: dict) -> int:
    return math.ceil(characters / policy["budgets"]["approximation_characters_per_token"])


def load_policy(path: Path = POLICY_PATH) -> dict:
    return _read_json(path)


def validate_policy(policy: Any, *, root: Path = ROOT,
                    inventory: dict | None = None) -> list[str]:
    if not isinstance(policy, dict):
        return ["policy_not_object"]
    errors: list[str] = []
    if set(policy) != ALLOWED_POLICY_KEYS:
        errors.append("policy_fields_invalid")
    if policy.get("schema") != POLICY_SCHEMA or policy.get("version") != "1.0.0":
        errors.append("policy_version_invalid")
    if policy.get("index_schema") != INDEX_SCHEMA \
            or policy.get("component_schema") != COMPONENT_SCHEMA:
        errors.append("policy_output_schemas_invalid")
    if policy.get("source_paths") != EXPECTED_SOURCE_PATHS:
        errors.append("policy_source_paths_invalid")
    if policy.get("safety") != SAFETY:
        errors.append("policy_safety_invalid")

    budgets = policy.get("budgets")
    expected_budget_keys = {
        "brief_max_characters", "index_max_characters", "component_max_characters",
        "approximation_characters_per_token", "summary_max_characters",
        "maximum_source_refs_per_component", "maximum_detail_list_items",
    }
    if not isinstance(budgets, dict) or set(budgets) != expected_budget_keys:
        errors.append("policy_budgets_invalid")
    elif (budgets.get("approximation_characters_per_token") != 3
          or not all(isinstance(budgets.get(key), int) and budgets[key] > 0
                     for key in expected_budget_keys)):
        errors.append("policy_budgets_invalid")

    domains = policy.get("domains")
    domain_ids: set[str] = set()
    if not isinstance(domains, list) or not domains:
        errors.append("policy_domains_invalid")
        domains = []
    for row in domains:
        if not isinstance(row, dict) or set(row) != {"id", "label", "summary"} \
                or not TOKEN_RE.fullmatch(str(row.get("id", ""))):
            errors.append("policy_domain_invalid")
            continue
        if row["id"] in domain_ids:
            errors.append("policy_domain_duplicate")
        domain_ids.add(row["id"])

    anchors = policy.get("anchors")
    anchor_ids: set[str] = set()
    if not isinstance(anchors, list) or not anchors:
        errors.append("policy_anchors_invalid")
        anchors = []
    for row in anchors:
        if not isinstance(row, dict) or set(row) != {
                "id", "domain", "label", "summary", "source_refs", "responsibilities"}:
            errors.append("policy_anchor_invalid")
            continue
        anchor_id = row.get("id")
        if not isinstance(anchor_id, str) or not TOKEN_RE.fullmatch(anchor_id) \
                or anchor_id in anchor_ids:
            errors.append("policy_anchor_id_invalid")
        anchor_ids.add(str(anchor_id))
        if row.get("domain") not in domain_ids:
            errors.append("policy_anchor_domain_invalid")
        refs = row.get("source_refs")
        if not isinstance(refs, list) or not refs:
            errors.append("policy_anchor_refs_invalid")
        else:
            for ref in refs:
                try:
                    _safe_source_path(root, ref)
                except ContextError:
                    errors.append(f"policy_anchor_ref_invalid:{ref}")
        responsibilities = row.get("responsibilities")
        if not isinstance(responsibilities, list) or not responsibilities \
                or not all(isinstance(item, str) and item.strip() for item in responsibilities):
            errors.append("policy_anchor_responsibilities_invalid")

    model_domains = policy.get("model_domains")
    if not isinstance(model_domains, dict) or not model_domains:
        errors.append("policy_model_domains_invalid")
        model_domains = {}
    elif any(not TOKEN_RE.fullmatch(str(name)) or domain not in domain_ids
             for name, domain in model_domains.items()):
        errors.append("policy_model_domains_invalid")
    if inventory is not None:
        inventory_names = {
            row.get("name") for row in inventory.get("entries", []) if isinstance(row, dict)}
        if set(model_domains) != inventory_names:
            errors.append("policy_model_inventory_mapping_incomplete")
    for key, relative in EXPECTED_SOURCE_PATHS.items():
        try:
            _safe_source_path(root, relative)
        except ContextError:
            errors.append(f"policy_source_unavailable:{key}")
    return sorted(set(errors))


def _validate_workflows(document: dict, root: Path) -> list[str]:
    errors: list[str] = []
    workflows = document.get("workflows")
    if document.get("schema") != "autonomy_workflows/1.1" \
            or document.get("safety", {}).get("execution_enabled") is not False \
            or not isinstance(workflows, dict) or not workflows:
        return ["workflow_contract_invalid"]
    names = set(workflows)
    for name, row in workflows.items():
        if not TOKEN_RE.fullmatch(str(name)) or not isinstance(row, dict):
            errors.append("workflow_entry_invalid")
            continue
        required = {"runner", "task_state_key", "launchd_label", "scheduled_pt",
                    "phase_gate", "timeout_seconds", "dependencies", "dependency_policy",
                    "artifact_contract", "run_id_prefix"}
        if set(row) != required:
            errors.append(f"workflow_fields_invalid:{name}")
            continue
        try:
            _safe_source_path(root, row["runner"])
        except ContextError:
            errors.append(f"workflow_runner_invalid:{name}")
        deps = row.get("dependencies")
        if not isinstance(deps, list) or any(dep not in names for dep in deps):
            errors.append(f"workflow_dependencies_invalid:{name}")
    return sorted(set(errors))


def _sources(root: Path, policy_path: Path | None = None) -> tuple[dict, dict, dict, dict]:
    selected_policy_path = policy_path or root / "config" / "agent_context.json"
    if selected_policy_path.is_symlink() or not selected_policy_path.is_file():
        raise ContextError("policy_missing_or_symlink")
    policy = load_policy(selected_policy_path)
    inventory = _read_json(_safe_source_path(root, EXPECTED_SOURCE_PATHS["model_inventory"]))
    policy_errors = validate_policy(policy, root=root, inventory=inventory)
    if policy_errors:
        raise ContextError("policy_invalid:" + ",".join(policy_errors))
    inventory_result = model_inventory.validate_inventory(inventory, root)
    if not inventory_result["ok"]:
        raise ContextError("model_inventory_invalid:" + ",".join(inventory_result["errors"]))
    workflows = _read_json(_safe_source_path(root, EXPECTED_SOURCE_PATHS["workflows"]))
    workflow_errors = _validate_workflows(workflows, root)
    if workflow_errors:
        raise ContextError("workflows_invalid:" + ",".join(workflow_errors))
    source_hashes = {"policy": sha256_file(selected_policy_path)}
    for key in SOURCE_KEYS:
        source_hashes[key] = sha256_file(
            _safe_source_path(root, policy["source_paths"][key]))
    return policy, inventory, workflows, source_hashes


def _source_refs(values: list[str], maximum: int) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) == maximum:
            break
    return seen


def _components(policy: dict, inventory: dict, workflows: dict) -> list[dict]:
    limit = policy["budgets"]["maximum_source_refs_per_component"]
    summary_limit = policy["budgets"]["summary_max_characters"]
    rows: list[dict] = []
    for anchor in policy["anchors"]:
        component_id = f"anchor.{anchor['id']}"
        rows.append({
            "id": component_id,
            "kind": "anchor",
            "domain": anchor["domain"],
            "label": anchor["label"],
            "summary": _bounded_text(anchor["summary"], summary_limit),
            "status": "configured",
            "source_refs": _source_refs(anchor["source_refs"], limit),
            "dependencies": [],
            "authority": "research_only_no_execution",
            "detail_command": f"python3 scripts/agent_context.py show --component {component_id} --format markdown",
        })
    for name, workflow in workflows["workflows"].items():
        component_id = f"workflow.{name}"
        summary = (
            f"Runs {workflow['runner']} at {workflow['scheduled_pt']} PT; "
            f"terminal contract {workflow['artifact_contract']} with a "
            f"{workflow['timeout_seconds']}-second timeout.")
        rows.append({
            "id": component_id,
            "kind": "workflow",
            "domain": "operations",
            "label": _label(name) + " Workflow",
            "summary": _bounded_text(summary, summary_limit),
            "status": "configured",
            "source_refs": _source_refs([
                workflow["runner"], "config/autonomy_workflows.json",
                f"launchd/{workflow['launchd_label']}.plist"], limit),
            "dependencies": [f"workflow.{dep}" for dep in workflow["dependencies"]],
            "authority": "research_only_no_execution",
            "detail_command": f"python3 scripts/agent_context.py show --component {component_id} --format markdown",
        })
    for entry in inventory["entries"]:
        name = entry["name"]
        component_id = f"model.{name}"
        rows.append({
            "id": component_id,
            "kind": "model",
            "domain": policy["model_domains"][name],
            "label": _label(name),
            "summary": _first_sentence(entry["purpose"], summary_limit),
            "status": entry["validation_status"],
            "source_refs": _source_refs(
                ["data/model_inventory.json", *entry["linked_tests"]], limit),
            "dependencies": [],
            "authority": "research_only_no_execution",
            "detail_command": f"python3 scripts/agent_context.py show --component {component_id} --format markdown",
        })
    return rows


def _brief_markdown(policy: dict, domains: list[dict], counts: dict) -> str:
    system = policy["system"]
    lines = [
        f"# {system['name']} - compact agent context",
        "",
        f"Purpose: {system['purpose']}",
        f"Stage: {system['current_stage']}",
        f"Profitability evidence: {system['profitability_state']}.",
        f"Execution: {system['execution_model']}; execution_enabled=false.",
        f"Fail-closed rule: {system['primary_constraint']}",
        "",
        f"Inventory: {counts['domains']} domains, {counts['components']} components "
        f"({counts['anchors']} anchors, {counts['workflows']} workflows, {counts['models']} models).",
        "",
        "## Domains",
    ]
    lines.extend(f"- {row['label']}: {row['summary']}" for row in domains)
    lines.extend([
        "",
        "## Read order",
        "1. Use this brief for orientation.",
        "2. Use the index only to locate a component.",
        "3. Request one component detail before reading source files.",
        "4. Read authoritative source files only when the bounded contract is insufficient.",
        "",
        "Commands: `python3 scripts/agent_context.py index --format json` and "
        "`python3 scripts/agent_context.py show --component <component-id> --format markdown`.",
        "",
        "This context is documentation and navigation only. It cannot authorize a Buy, change "
        "strategy or risk limits, establish profitability, or place/preview/simulate/modify/cancel an order.",
    ])
    return "\n".join(lines) + "\n"


def _finalize_index(document: dict, policy: dict) -> dict:
    budgets = policy["budgets"]
    brief_chars = len(document["brief_markdown"])
    document["budgets"] = {
        "approximation_method": "ceil_unicode_characters_divided_by_3",
        "brief_characters": brief_chars,
        "brief_approximate_tokens": _approx_tokens(brief_chars, policy),
        "brief_max_characters": budgets["brief_max_characters"],
        "index_characters": 0,
        "index_approximate_tokens": 0,
        "index_max_characters": budgets["index_max_characters"],
        "component_max_characters": budgets["component_max_characters"],
    }
    document["artifact_sha256"] = "0" * 64
    for _ in range(12):
        characters = len(canonical(document))
        tokens = _approx_tokens(characters, policy)
        if (document["budgets"]["index_characters"],
                document["budgets"]["index_approximate_tokens"]) == (characters, tokens):
            break
        document["budgets"]["index_characters"] = characters
        document["budgets"]["index_approximate_tokens"] = tokens
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    document["artifact_sha256"] = sha256_obj(body)
    return document


def build_index(*, root: Path = ROOT, policy_path: Path | None = None) -> dict:
    root = root.resolve()
    policy, inventory, workflows, source_hashes = _sources(root, policy_path)
    components = _components(policy, inventory, workflows)
    domain_counts = {row["id"]: 0 for row in policy["domains"]}
    for component in components:
        domain_counts[component["domain"]] += 1
    domains = [
        {**row, "component_count": domain_counts[row["id"]]}
        for row in policy["domains"]
    ]
    counts = {
        "domains": len(domains),
        "components": len(components),
        "anchors": len(policy["anchors"]),
        "workflows": len(workflows["workflows"]),
        "models": len(inventory["entries"]),
    }
    document = {
        "schema": INDEX_SCHEMA,
        "system": copy.deepcopy(policy["system"]),
        "source_hashes": source_hashes,
        "counts": counts,
        "domains": domains,
        "components": components,
        "brief_markdown": _brief_markdown(policy, domains, counts),
        "agent_usage": {
            "brief": "python3 scripts/agent_context.py brief --format markdown",
            "index": "python3 scripts/agent_context.py index --format json",
            "component": "python3 scripts/agent_context.py show --component <component-id> --format markdown",
        },
        "safety": copy.deepcopy(SAFETY),
    }
    document = _finalize_index(document, policy)
    errors = validate_index(document, policy=policy)
    if errors:
        raise ContextError("index_self_validation_failed:" + ",".join(errors))
    return document


def validate_index(document: Any, *, policy: dict | None = None) -> list[str]:
    if not isinstance(document, dict):
        return ["index_not_object"]
    errors: list[str] = []
    expected_keys = {"schema", "system", "source_hashes", "counts", "budgets", "domains",
                     "components", "brief_markdown", "agent_usage", "safety", "artifact_sha256"}
    if set(document) != expected_keys or document.get("schema") != INDEX_SCHEMA:
        errors.append("index_contract_invalid")
    if document.get("safety") != SAFETY:
        errors.append("index_safety_invalid")
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    if document.get("artifact_sha256") != sha256_obj(body):
        errors.append("index_artifact_sha_mismatch")
    encoded = canonical(document)
    if EMAIL_RE.search(encoded):
        errors.append("index_email_present")
    if MASKED_ACCOUNT_RE.search(encoded):
        errors.append("index_account_shape_present")
    if SECRET_VALUE_RE.search(encoded):
        errors.append("index_secret_shape_present")
    components = document.get("components")
    domains = document.get("domains")
    if not isinstance(components, list) or not isinstance(domains, list):
        errors.append("index_collections_invalid")
        return sorted(set(errors))
    domain_ids = {row.get("id") for row in domains if isinstance(row, dict)}
    component_ids = {row.get("id") for row in components if isinstance(row, dict)}
    if len(component_ids) != len(components):
        errors.append("index_component_ids_invalid")
    for row in components:
        if not isinstance(row, dict) or not COMPONENT_RE.fullmatch(str(row.get("id", ""))):
            errors.append("index_component_invalid")
            continue
        if row.get("domain") not in domain_ids or row.get("authority") != "research_only_no_execution":
            errors.append("index_component_authority_or_domain_invalid")
        if any(dep not in component_ids for dep in row.get("dependencies", [])):
            errors.append("index_component_dependency_invalid")
    counts = document.get("counts", {})
    if counts.get("domains") != len(domains) or counts.get("components") != len(components):
        errors.append("index_counts_invalid")
    budgets = document.get("budgets")
    if not isinstance(budgets, dict):
        errors.append("index_budgets_invalid")
    else:
        if budgets.get("brief_characters") != len(str(document.get("brief_markdown", ""))):
            errors.append("index_brief_measurement_invalid")
        if budgets.get("index_characters") != len(encoded):
            errors.append("index_measurement_invalid")
        if isinstance(budgets.get("index_max_characters"), int) \
                and len(encoded) > budgets["index_max_characters"]:
            errors.append("index_budget_exceeded")
        if isinstance(budgets.get("brief_max_characters"), int) \
                and len(str(document.get("brief_markdown", ""))) > budgets["brief_max_characters"]:
            errors.append("brief_budget_exceeded")
    if policy is not None:
        if budgets and budgets.get("index_max_characters") != policy["budgets"]["index_max_characters"]:
            errors.append("index_policy_budget_mismatch")
        if document.get("system") != policy.get("system"):
            errors.append("index_system_policy_mismatch")
    return sorted(set(errors))


def _bounded_list(values: Any, policy: dict) -> list[str]:
    if not isinstance(values, list):
        return []
    maximum = policy["budgets"]["maximum_detail_list_items"]
    return [_bounded_text(value, 420) for value in values[:maximum]]


def _finalize_component(document: dict, policy: dict) -> dict:
    maximum = policy["budgets"]["component_max_characters"]
    document["budget"] = {
        "approximation_method": "ceil_unicode_characters_divided_by_3",
        "characters": 0,
        "approximate_tokens": 0,
        "max_characters": maximum,
    }
    document["artifact_sha256"] = "0" * 64
    for _ in range(12):
        characters = len(canonical(document))
        tokens = _approx_tokens(characters, policy)
        if (document["budget"]["characters"], document["budget"]["approximate_tokens"]) \
                == (characters, tokens):
            break
        document["budget"]["characters"] = characters
        document["budget"]["approximate_tokens"] = tokens
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    document["artifact_sha256"] = sha256_obj(body)
    if len(canonical(document)) > maximum:
        raise ContextError("component_budget_exceeded")
    return document


def build_component(component_id: str, *, root: Path = ROOT,
                    policy_path: Path | None = None) -> dict:
    policy, inventory, workflows, source_hashes = _sources(root.resolve(), policy_path)
    index = build_index(root=root, policy_path=policy_path)
    component = next((row for row in index["components"] if row["id"] == component_id), None)
    if component is None:
        raise ContextError("component_not_found")
    kind, name = component_id.split(".", 1)
    if kind == "anchor":
        source = next(row for row in policy["anchors"] if row["id"] == name)
        detail = {
            "summary": source["summary"],
            "responsibilities": _bounded_list(source["responsibilities"], policy),
            "authoritative_sources": component["source_refs"],
            "non_authority": "Navigation context only; source contracts remain authoritative.",
        }
    elif kind == "workflow":
        source = workflows["workflows"][name]
        detail = {
            "runner": source["runner"],
            "scheduled_pt": source["scheduled_pt"],
            "phase_gate": source["phase_gate"],
            "timeout_seconds": source["timeout_seconds"],
            "dependencies": source["dependencies"],
            "dependency_policy": source["dependency_policy"],
            "artifact_contract": source["artifact_contract"],
            "activation": workflows["activation"],
            "non_authority": "Scheduling a research workflow never authorizes execution.",
        }
    else:
        source = next(row for row in inventory["entries"] if row["name"] == name)
        detail = {
            "purpose": _bounded_text(source["purpose"], 720),
            "type": source["type"],
            "owner": source["owner"],
            "inputs": _bounded_list(source["inputs"], policy),
            "outputs": _bounded_list(source["outputs"], policy),
            "limitations": _bounded_list(source["limitations"], policy),
            "linked_tests": _bounded_list(source["linked_tests"], policy),
            "approved_use": _bounded_text(source["approved_use"], 720),
            "prohibited_use": _bounded_text(source["prohibited_use"], 720),
            "review_after": source["review_after"],
        }
    document = {
        "schema": COMPONENT_SCHEMA,
        "component": component,
        "detail": detail,
        "source_hashes": source_hashes,
        "safety": copy.deepcopy(SAFETY),
    }
    return _finalize_component(document, policy)


def validate_component(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["component_not_object"]
    errors: list[str] = []
    expected = {"schema", "component", "detail", "source_hashes", "budget", "safety", "artifact_sha256"}
    if set(document) != expected or document.get("schema") != COMPONENT_SCHEMA:
        errors.append("component_contract_invalid")
    if document.get("safety") != SAFETY:
        errors.append("component_safety_invalid")
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    if document.get("artifact_sha256") != sha256_obj(body):
        errors.append("component_artifact_sha_mismatch")
    encoded = canonical(document)
    budget = document.get("budget", {})
    if budget.get("characters") != len(encoded):
        errors.append("component_measurement_invalid")
    if isinstance(budget.get("max_characters"), int) and len(encoded) > budget["max_characters"]:
        errors.append("component_budget_exceeded")
    if EMAIL_RE.search(encoded) or MASKED_ACCOUNT_RE.search(encoded) or SECRET_VALUE_RE.search(encoded):
        errors.append("component_private_shape_present")
    return sorted(set(errors))


def _index_markdown(document: dict) -> str:
    lines = [document["brief_markdown"].rstrip(), "", "## Component index"]
    for domain in document["domains"]:
        lines.extend(["", f"### {domain['label']} ({domain['component_count']})"])
        for row in document["components"]:
            if row["domain"] == domain["id"]:
                lines.append(f"- `{row['id']}` [{row['status']}]: {row['summary']}")
    return "\n".join(lines) + "\n"


def _component_markdown(document: dict) -> str:
    component = document["component"]
    lines = [
        f"# {component['label']}", "",
        f"- ID: `{component['id']}`",
        f"- Domain: `{component['domain']}`",
        f"- Kind/status: `{component['kind']}` / `{component['status']}`",
        f"- Authority: `{component['authority']}`",
        f"- Approximate tokens: {document['budget']['approximate_tokens']}",
        "", component["summary"], "", "## Contract",
    ]
    for key, value in document["detail"].items():
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            lines.append(f"\n### {label}")
            lines.extend(f"- {item}" for item in value)
        elif isinstance(value, dict):
            lines.append(f"- {label}: `{canonical(value)}`")
        else:
            lines.append(f"- {label}: {value}")
    lines.extend(["", "## Source refs", *[f"- `{ref}`" for ref in component["source_refs"]]])
    return "\n".join(lines) + "\n"


def _emit(document: dict, output_format: str, markdown: str) -> None:
    if output_format == "markdown":
        print(markdown, end="")
    else:
        print(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded local agent-context compiler.")
    parser.add_argument("--root", default=str(ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    for name in ("brief", "index"):
        child = subparsers.add_parser(name)
        child.add_argument("--format", choices=("json", "markdown"), default="json")
    show = subparsers.add_parser("show")
    show.add_argument("--component", required=True)
    show.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        index = build_index(root=root)
        if args.command == "check":
            print(json.dumps({
                "ok": True,
                "schema": INDEX_SCHEMA,
                "counts": index["counts"],
                "budgets": index["budgets"],
                "artifact_sha256": index["artifact_sha256"],
                "safety": index["safety"],
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "brief":
            brief = {
                "schema": BRIEF_SCHEMA,
                "brief_markdown": index["brief_markdown"],
                "counts": index["counts"],
                "source_hashes": index["source_hashes"],
                "budget": {
                    "characters": index["budgets"]["brief_characters"],
                    "approximate_tokens": index["budgets"]["brief_approximate_tokens"],
                    "max_characters": index["budgets"]["brief_max_characters"],
                },
                "safety": index["safety"],
            }
            _emit(brief, args.format, index["brief_markdown"])
            return 0
        if args.command == "index":
            _emit(index, args.format, _index_markdown(index))
            return 0
        component = build_component(args.component, root=root)
        errors = validate_component(component)
        if errors:
            raise ContextError("component_self_validation_failed:" + ",".join(errors))
        _emit(component, args.format, _component_markdown(component))
        return 0
    except ContextError as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "execution_enabled": False,
            "network_invoked": False,
            "model_invoked": False,
            "broker_invoked": False,
        }, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
