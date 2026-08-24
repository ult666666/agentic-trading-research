#!/usr/bin/env python3
"""Build a deterministic, hash-bound catalyst/event graph from validated evidence.

The graph is a diagnostic research artifact. It makes event-to-symbol and event-to-claim joins
explicit, deduplicates common-origin sources, preserves conflicts, and emits deterministic
confirmed/partial/conflicted/unverified verdicts. It never establishes a price, changes a score,
grants Buy authority, activates a strategy, updates a ledger, or touches a broker/model/network.

CLI:
  catalyst_graph.py build --target YYYY-MM-DD [--evidence PATH] [--out PATH]
  catalyst_graph.py validate --graph PATH [--evidence PATH]
  catalyst_graph.py summary --graph PATH
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import evidence_schema  # noqa: E402

GRAPH_SCHEMA = "catalyst_graph/1.0"
TAXONOMY_SCHEMA = "catalyst_taxonomy/1.0"
DEFAULT_TAXONOMY = ROOT / "config" / "catalyst_taxonomy.json"
SAFETY = {
    "diagnostic_only": True,
    "research_only": True,
    "execution_enabled": False,
    "feeds_buy_gate": False,
    "feeds_production_scoring": False,
    "strategy_activation_allowed": False,
    "grants_actionability": False,
}
VERDICTS = ("confirmed", "partial", "conflicted", "unverified")
RELATIONS = {
    "affects", "issued_by", "issues", "confirms", "supports", "contradicts",
    "syndicated_copy", "same_origin",
}
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_UNSAFE_TEXT = [
    re.compile(r"https?://\S+", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b\d{9,}\b"),
    re.compile(r"(?i)\b(?:bearer|access_token|refresh_token|password|secret|sk-|rh-)[A-Za-z0-9._-]{4,}"),
]
_FORBIDDEN_KEYS = {
    "account", "account_id", "account_number", "balance", "buying_power", "cash",
    "cookie", "credential", "password", "position", "secret", "token",
}


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str | None:
    try:
        return _sha_bytes(path.read_bytes())
    except Exception:
        return None


def _canonical_hash(doc: dict) -> str:
    return _sha_bytes(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


def _token(value) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return token or "other"


def _safe_label(value, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit or any(p.search(text) for p in _UNSAFE_TEXT):
        raise ValueError("unsafe_or_invalid_event_label")
    return text


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def load_taxonomy(path: Path = DEFAULT_TAXONOMY) -> tuple[dict, dict[str, str], str]:
    raw = path.read_bytes()
    doc = json.loads(raw)
    errors = []
    if not isinstance(doc, dict) or doc.get("schema") != TAXONOMY_SCHEMA:
        errors.append("taxonomy_schema_invalid")
    categories = doc.get("categories") if isinstance(doc.get("categories"), dict) else {}
    unknown = doc.get("unknown_category")
    if unknown not in categories:
        errors.append("taxonomy_unknown_category_missing")
    if doc.get("verdicts") != list(VERDICTS):
        errors.append("taxonomy_verdicts_invalid")
    safety = doc.get("safety") if isinstance(doc.get("safety"), dict) else {}
    expected_safety = {
        "research_only": True,
        "execution_enabled": False,
        "feeds_buy_gate": False,
        "feeds_production_scoring": False,
        "strategy_activation_allowed": False,
        "unknown_type_fails_to_other": True,
        "explicit_claim_links_required": True,
    }
    if safety != expected_safety:
        errors.append("taxonomy_safety_contract_invalid")
    aliases: dict[str, str] = {}
    for category, rec in categories.items():
        if not isinstance(category, str) or not isinstance(rec, dict):
            errors.append("taxonomy_category_invalid")
            continue
        if not isinstance(rec.get("binary_event"), bool) or not isinstance(
                rec.get("dilution_financing"), bool):
            errors.append(f"taxonomy_flags_invalid:{category}")
        vals = rec.get("aliases")
        if not isinstance(vals, list) or not vals:
            errors.append(f"taxonomy_aliases_invalid:{category}")
            continue
        for alias in vals:
            key = _token(alias)
            if key in aliases and aliases[key] != category:
                errors.append(f"taxonomy_alias_collision:{key}")
            aliases[key] = category
    if errors:
        raise ValueError(";".join(errors))
    return doc, aliases, _sha_bytes(raw)


def _event_id(target: str, cat: dict, category: str) -> str:
    identity = {
        "target": target,
        "symbol": cat.get("symbol"),
        "category": category,
        "event_date": cat.get("event_date"),
        "announcement_date": cat.get("announcement_date"),
        "event_label_sha256": _sha_bytes(str(cat.get("event") or "").encode()),
    }
    return "event:" + _canonical_hash(identity)[:20]


def _edge(source: str, target: str, relation: str, **facts) -> dict:
    if relation not in RELATIONS:
        raise ValueError(f"unknown_relation:{relation}")
    doc = {"from": source, "to": target, "relation": relation, **facts}
    doc["id"] = "edge:" + _canonical_hash(doc)[:20]
    return doc


def _linked_conflict_ids(validation: dict) -> set[str]:
    out: set[str] = set()
    for row in validation.get("unresolved_material_conflicts") or []:
        if not isinstance(row, dict):
            continue
        for key in ("claim_a", "claim_b"):
            if row.get(key) is not None:
                out.add(str(row[key]))
    return out


def _event_verdict(cat: dict, linked: list[dict], quorum: dict,
                   unresolved_claim_ids: set[str], category_flags: dict) -> dict:
    linked_ids = {str(c.get("claim_id")) for c in linked}
    conflict = bool(cat.get("source_conflict") is True
                    or linked_ids & unresolved_claim_ids
                    or any(c.get("validation_status") == "conflicted" for c in linked))
    secondary_present = any(
        c.get("rumored") is not True
        and evidence_schema._claim_has_catalyst_role(c, "secondary")
        for c in linked
    )
    if conflict:
        verdict = "conflicted"
    elif quorum.get("rumored"):
        verdict = "unverified"
    elif quorum.get("effective_primary"):
        verdict = "confirmed"
    elif secondary_present:
        verdict = "partial"
    else:
        verdict = "unverified"
    return {
        "verdict": verdict,
        "evidence_strength": quorum.get("strength"),
        "effective_primary": bool(quorum.get("effective_primary")),
        "effective_independent_sources": int(quorum.get("effective_independent") or 0),
        "linked_claim_count": len(linked),
        "linked_claim_ids": sorted(linked_ids),
        "flags": {
            "issuer_only": cat.get("issuer_only") is True,
            "stale_or_expired": cat.get("catalyst_expired") is True,
            "rumored": quorum.get("rumored") is True,
            "unresolved_conflict": conflict,
            "binary_event": category_flags.get("binary_event") is True,
            "dilution_financing": category_flags.get("dilution_financing") is True,
            "already_priced": None,
            "already_priced_status": "not_deterministically_available",
        },
        "grants_actionability": False,
    }


def build_graph(target: str, *, evidence_path: Path, taxonomy_path: Path = DEFAULT_TAXONOMY,
                root: Path = ROOT) -> dict:
    taxonomy, aliases, taxonomy_sha = load_taxonomy(taxonomy_path)
    evidence_bytes = evidence_path.read_bytes()
    evidence_sha = _sha_bytes(evidence_bytes)
    try:
        evidence = json.loads(evidence_bytes)
    except Exception:
        evidence = None
    validation = evidence_schema.validate_evidence(
        evidence, target, research_dir=evidence_path.parent)

    validation_errors = list(validation.get("errors") or [])
    validation_blockers = list(validation.get("hard_blockers") or [])
    # A catalyst-authority blocker means "no catalyst may be treated as confirmed", which is a
    # DIAGNOSTIC state for this graph, not a structural fault: the graph is still built, every
    # source stays `lead_only`, and no edge asserts `confirms`. A structural fault (schema
    # errors, unusable evidence) is what blocks.
    #
    # 2026-08-04: the token set is imported from evidence_schema rather than re-literalled here.
    # This line previously matched only `catalyst_quorum_insufficient:` and silently downgraded
    # the whole graph to `blocked` the moment that vocabulary changed — the same
    # hardcoded-constant coupling that caused the namespace outage, one file over.
    def _is_catalyst_authority_blocker(item) -> bool:
        text = str(item)
        return (text.startswith("catalyst_quorum_insufficient:")
                or text == evidence_schema.CATALYST_AUTHORITY_STRUCTURAL_BLOCKER)

    diagnostic_unverified = (not validation_errors and bool(validation_blockers)
                             and all(_is_catalyst_authority_blocker(item)
                                     for item in validation_blockers))
    build_status = ("ready" if validation.get("ok") else
                    "diagnostic_only_unverified" if diagnostic_unverified else "blocked")
    base = {
        "schema": GRAPH_SCHEMA,
        "target_session_date": target,
        "source_evidence_path": _safe_rel(evidence_path, root),
        "source_evidence_sha256": evidence_sha,
        "source_evidence_generated_utc": evidence.get("generated_utc") if isinstance(evidence, dict) else None,
        "taxonomy_path": _safe_rel(taxonomy_path, root),
        "taxonomy_sha256": taxonomy_sha,
        "taxonomy_version": taxonomy.get("version"),
        "input_evidence_valid": bool(validation.get("ok")),
        "input_evidence_structurally_usable_for_diagnostics": bool(
            validation.get("ok") or diagnostic_unverified),
        "build_status": build_status,
        "blockers": sorted(set(validation_errors + validation_blockers)),
        "nodes": [],
        "edges": [],
        "event_verdicts": [],
        "summary": {},
        "safety": dict(SAFETY),
    }
    if build_status == "blocked":
        base["summary"] = {"event_count": 0, "node_count": 0, "edge_count": 0,
                           "verdict_counts": {k: 0 for k in VERDICTS}}
        base["graph_sha256"] = _canonical_hash(base)
        return base

    claims = evidence.get("claims") or []
    catalysts = evidence.get("catalysts") or []
    by_claim = {str(c.get("claim_id")): c for c in claims if isinstance(c, dict)}
    unresolved_ids = _linked_conflict_ids(validation)
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    verdicts: list[dict] = []
    source_groups: dict[str, list[str]] = {}

    def add_node(node: dict) -> None:
        existing = nodes.get(node["id"])
        if existing is not None and existing != node:
            raise ValueError(f"node_identity_collision:{node['id']}")
        nodes[node["id"]] = node

    def add_edge(edge: dict) -> None:
        edges[edge["id"]] = edge

    quorums = validation.get("catalyst_quorums") or []
    for idx, cat in enumerate(catalysts):
        raw_type = _token(cat.get("catalyst_type"))
        category = aliases.get(raw_type, taxonomy["unknown_category"])
        cat_flags = taxonomy["categories"][category]
        event_id = _event_id(target, cat, category)
        label = _safe_label(cat.get("event"))
        event_node = {
            "id": event_id,
            "type": "event",
            "event_category": category,
            "raw_catalyst_type_token": raw_type,
            "unknown_type_mapped_to_other": raw_type not in aliases,
            "symbol": cat.get("symbol"),
            "untrusted_data_label": label,
            "announcement_date": cat.get("announcement_date"),
            "event_date": cat.get("event_date"),
            "effective_time": cat.get("effective_time"),
            "declared_status": cat.get("status"),
        }
        add_node(event_node)

        symbol = cat.get("symbol")
        if symbol is not None:
            if not _SYMBOL.fullmatch(symbol):
                raise ValueError("invalid_symbol_after_evidence_validation")
            security_id, issuer_id = f"security:{symbol}", f"issuer:{symbol}"
            add_node({"id": security_id, "type": "security", "symbol": symbol,
                      "identity_precision": "symbol_for_target_session"})
            add_node({"id": issuer_id, "type": "issuer", "symbol": symbol,
                      "identity_precision": "symbol_proxy_not_permanent_entity_id"})
            add_edge(_edge(event_id, security_id, "affects"))
            add_edge(_edge(event_id, issuer_id, "issued_by"))
            add_edge(_edge(issuer_id, security_id, "issues"))

        linked = [by_claim[cid] for cid in cat.get("claim_ids", []) if cid in by_claim]
        for claim in linked:
            claim_id = str(claim["claim_id"])
            source_id = "source:" + _sha_bytes(claim_id.encode())[:20]
            group = str(claim.get("corroboration_group") or "")
            position = str(claim.get("primary_or_secondary") or "")
            authority_reason = evidence_schema._claim_catalyst_authority_reason(
                claim, position) if position in {"primary", "secondary"} \
                else "source_position_mismatch"
            source_node = {
                "id": source_id,
                "type": "source_document",
                "evidence_claim_id": claim_id,
                "source_domain": claim.get("source_domain"),
                "source_type": claim.get("source_type"),
                "source_authority_tier": claim.get("source_authority_tier"),
                "primary_or_secondary": claim.get("primary_or_secondary"),
                "corroboration_group": group,
                "publication_timestamp": claim.get("publication_timestamp"),
                "event_effective_timestamp": claim.get("event_effective_timestamp"),
                "observed_at_utc": claim.get("retrieval_timestamp_utc"),
                "validation_status": claim.get("validation_status"),
                "catalyst_authority_status": (
                    "authorized" if authority_reason == "authorized" else "lead_only"),
                "catalyst_authority_reason": authority_reason,
                "untrusted_data_reference": f"evidence.json#claim_id={claim_id}",
            }
            add_node(source_node)
            source_groups.setdefault(group, []).append(source_id)
            if claim.get("contradicts_thesis") is True or claim_id in unresolved_ids:
                relation = "contradicts"
            elif claim.get("primary_or_secondary") == "primary" \
                    and authority_reason == "authorized":
                relation = "confirms"
            else:
                relation = "supports"
            add_edge(_edge(source_id, event_id, relation, evidence_claim_id=claim_id))

        quorum = quorums[idx] if idx < len(quorums) else evidence_schema.catalyst_quorum(cat, claims)
        verdict = _event_verdict(cat, linked, quorum, unresolved_ids, cat_flags)
        verdict.update({"event_id": event_id, "symbol": symbol, "event_category": category})
        verdicts.append(verdict)

    # Explicit common-origin graph. Copies in one corroboration group never become independent.
    for group, ids in source_groups.items():
        unique = sorted(set(ids))
        if not group or len(unique) < 2:
            continue
        canonical = min(unique, key=lambda sid: (
            int(nodes[sid].get("source_authority_tier") or 99),
            0 if nodes[sid].get("primary_or_secondary") == "primary" else 1,
            sid,
        ))
        for sid in unique:
            if sid != canonical:
                add_edge(_edge(sid, canonical, "syndicated_copy",
                               corroboration_group=group))

    # Preserve explicit claim-to-claim conflicts when both endpoints occur in the graph.
    claim_source = {n["evidence_claim_id"]: n["id"] for n in nodes.values()
                    if n.get("type") == "source_document"}
    seen_pairs: set[frozenset[str]] = set()
    for claim in claims:
        if not isinstance(claim, dict) or str(claim.get("claim_id")) not in claim_source:
            continue
        a = str(claim["claim_id"])
        for other in claim.get("conflicts_with") or []:
            b = str(other)
            pair = frozenset((a, b))
            if b not in claim_source or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            src, dst = sorted((claim_source[a], claim_source[b]))
            add_edge(_edge(src, dst, "contradicts", conflict_preserved=True))

    verdict_counts = {k: sum(v["verdict"] == k for v in verdicts) for k in VERDICTS}
    base.update({
        "nodes": sorted(nodes.values(), key=lambda x: x["id"]),
        "edges": sorted(edges.values(), key=lambda x: x["id"]),
        "event_verdicts": sorted(verdicts, key=lambda x: x["event_id"]),
        "summary": {
            "event_count": len(verdicts),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "verdict_counts": verdict_counts,
            "binary_event_count": sum(v["flags"]["binary_event"] for v in verdicts),
            "dilution_financing_count": sum(v["flags"]["dilution_financing"] for v in verdicts),
            "stale_or_expired_count": sum(v["flags"]["stale_or_expired"] for v in verdicts),
            "unresolved_conflict_count": sum(v["flags"]["unresolved_conflict"] for v in verdicts),
        },
    })
    base["graph_sha256"] = _canonical_hash(base)
    return base


def validate_graph(graph: dict, *, root: Path = ROOT, evidence_path: Path | None = None,
                   taxonomy_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(graph, dict) or graph.get("schema") != GRAPH_SCHEMA:
        return ["graph_schema_invalid"]
    recorded_hash = graph.get("graph_sha256")
    unhashed = dict(graph)
    unhashed.pop("graph_sha256", None)
    if recorded_hash != _canonical_hash(unhashed):
        errors.append("graph_self_hash_mismatch")
    if graph.get("safety") != SAFETY:
        errors.append("graph_safety_contract_invalid")
    ev_path = evidence_path
    if ev_path is None:
        ev_path = Path(str(graph.get("source_evidence_path") or ""))
        if not ev_path.is_absolute():
            ev_path = root / ev_path
    tx_path = taxonomy_path
    if tx_path is None:
        tx_path = Path(str(graph.get("taxonomy_path") or ""))
        if not tx_path.is_absolute():
            tx_path = root / tx_path
    if _sha_file(ev_path) != graph.get("source_evidence_sha256"):
        errors.append("source_evidence_hash_mismatch")
    if _sha_file(tx_path) != graph.get("taxonomy_sha256"):
        errors.append("taxonomy_hash_mismatch")
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    node_ids = [n.get("id") for n in nodes if isinstance(n, dict)]
    edge_ids = [e.get("id") for e in edges if isinstance(e, dict)]
    if len(node_ids) != len(nodes) or len(set(node_ids)) != len(node_ids):
        errors.append("node_ids_invalid_or_duplicate")
    if len(edge_ids) != len(edges) or len(set(edge_ids)) != len(edge_ids):
        errors.append("edge_ids_invalid_or_duplicate")
    known = set(node_ids)
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") not in known or edge.get("to") not in known:
            errors.append("edge_endpoint_missing")
        elif edge.get("relation") not in RELATIONS:
            errors.append("edge_relation_invalid")
    event_ids = {n["id"] for n in nodes if n.get("type") == "event"}
    for row in graph.get("event_verdicts") or []:
        if not isinstance(row, dict) or row.get("event_id") not in event_ids:
            errors.append("verdict_event_missing")
        elif row.get("verdict") not in VERDICTS or row.get("grants_actionability") is not False:
            errors.append("verdict_contract_invalid")

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in _FORBIDDEN_KEYS:
                    errors.append(f"forbidden_key:{key}")
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    walk(graph)
    return sorted(set(errors))


def _write_once(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return "idempotent_existing"
        raise ValueError("immutable_catalyst_graph_conflict")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise ValueError("immutable_catalyst_graph_conflict") from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return "written"


def build_for_target(target: str, *, root: Path = ROOT, evidence_path: Path | None = None,
                     taxonomy_path: Path = DEFAULT_TAXONOMY, out: Path | None = None) -> dict:
    evidence_path = evidence_path or evidence_schema.evidence_path(target, root)
    out = out or (root / "data" / "runtime" / "research" / target / "catalyst_graph.json")
    graph = build_graph(target, evidence_path=evidence_path, taxonomy_path=taxonomy_path, root=root)
    errors = validate_graph(graph, root=root, evidence_path=evidence_path,
                            taxonomy_path=taxonomy_path)
    if errors:
        raise ValueError("graph_validation_failed:" + ",".join(errors))
    action = _write_once(out, json.dumps(graph, indent=2, sort_keys=True) + "\n")
    return {"ok": graph["build_status"] != "blocked", "action": action,
            "build_status": graph["build_status"], "path": str(out),
            "graph_sha256": graph["graph_sha256"], "summary": graph["summary"],
            "blockers": graph["blockers"], "diagnostic_only": True,
            "execution_enabled": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic catalyst/event graph.")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--target", required=True)
    b.add_argument("--root")
    b.add_argument("--evidence")
    b.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY))
    b.add_argument("--out")
    v = sub.add_parser("validate")
    v.add_argument("--graph", required=True)
    v.add_argument("--root")
    v.add_argument("--evidence")
    v.add_argument("--taxonomy")
    s = sub.add_parser("summary")
    s.add_argument("--graph", required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "build":
            root = Path(args.root) if args.root else ROOT
            result = build_for_target(
                args.target, root=root,
                evidence_path=Path(args.evidence) if args.evidence else None,
                taxonomy_path=Path(args.taxonomy),
                out=Path(args.out) if args.out else None,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        graph_path = Path(args.graph)
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        if args.command == "validate":
            root = Path(args.root) if args.root else ROOT
            errors = validate_graph(
                graph, root=root,
                evidence_path=Path(args.evidence) if args.evidence else None,
                taxonomy_path=Path(args.taxonomy) if args.taxonomy else None,
            )
            print(json.dumps({"ok": not errors, "errors": errors}, indent=2, sort_keys=True))
            return 0 if not errors else 1
        print(json.dumps({"ok": graph.get("build_status") != "blocked",
                          "target_session_date": graph.get("target_session_date"),
                          "build_status": graph.get("build_status"),
                          "summary": graph.get("summary"),
                          "blockers": graph.get("blockers"),
                          "diagnostic_only": True, "execution_enabled": False},
                         indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_class": type(exc).__name__,
                          "reason": str(exc), "diagnostic_only": True,
                          "execution_enabled": False}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
