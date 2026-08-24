#!/usr/bin/env python3
"""Sanitized, read-only morning-report view models for the local Operator Console.

This module never runs research and never derives a trade plan.  It selects an already-written
artifact through :mod:`report_source_adapter`, revalidates finality, and exposes only fields that
the authoritative bytes or their receipt support.  Missing fields remain absent or ``未验证``.

No network, broker, account, credential, model, order, or execution surface is imported here.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import freeze_completion_probe
import legacy_morning_receipt
import morning_audit_supervisor
from lib.report_source_adapter import (
    STATUS_OFFICIAL,
    STATUS_OFFICIAL_NO_TRADE,
    STATUS_PRELIMINARY_NO_BUY,
    STATUS_REPORT_FAILURE,
    SourceDocument,
    SourceError,
    failure_document,
    load_source,
    sha256_file,
)

REPORT_SCHEMA = "morning_report_console/1.0"
HISTORY_SCHEMA = "morning_report_history/1.0"
TARGET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRELIM_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_.+_(?P<hhmm>\d{4})PT\.md$", re.IGNORECASE)
TICKER_RE = re.compile(
    r"^\s*(?:#{2,4}\s*)?(?:\d+[.)]\s*)?\*{0,2}([A-Z]{1,5})\*{0,2}\s*(?:[—–-]|\s\|)")
STATUS_NOT_READY = "NOT READY"

STATUS_LABELS = {
    STATUS_OFFICIAL: "OFFICIAL",
    STATUS_OFFICIAL_NO_TRADE: "OFFICIAL — NO TRADE",
    STATUS_PRELIMINARY_NO_BUY: "PRELIMINARY — NO BUY",
    STATUS_REPORT_FAILURE: "REPORT FAILURE",
    STATUS_NOT_READY: "NOT READY",
}

SAFETY = {
    "read_only": True,
    "loopback_only": True,
    "external_network_enabled": False,
    "external_communications_enabled": False,
    "account_data_allowed": False,
    "report_content_allowed": True,
    "sanitized_report_content_only": True,
    "credentials_allowed": False,
    "raw_broker_data_allowed": False,
    "model_invoked": False,
    "broker_invoked": False,
    "execution_enabled": False,
    "options_execution_enabled": False,
}

PUBLIC_SECTIONS = {
    "__preamble__",
    "system status",
    "market regime",
    "portfolio risk alerts",
    "best actionable setups",
    "best market setup",
    "watchlist",
    "avoid or reject",
    "final decision",
    "user action ticket",
    "buy block reasons",
    "sources",
    "data quality",
    "catalyst window",
    "catalysts",
    "verified facts",
    "facts",
    "assumptions",
    "speculation",
    "unknown",
    "unknowns",
}

# A match fails the report closed.  These are shapes that cannot be returned to the browser even
# if a malformed report was accidentally written into an approved report directory.
SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:access_token|refresh_token|client_secret|client_id)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|api[_ -]?key)\b\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\baccount[_ -]?(?:number|id)\b\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(
        r"\baccount(?:[_ -]?(?:number|id))?\b\s*[:=]\s*(?:\*{2,}|•{2,})[- ]?\d{2,4}",
        re.IGNORECASE),
    re.compile(r"\b(?:account_value|buying_power|cash_balance|holding_qty)\b\s*[:=]", re.IGNORECASE),
    re.compile(r'"(?:positions|orders|holdings)"\s*:', re.IGNORECASE),
    re.compile(r"\braw[_ -]?(?:broker|account|order)[_ -]?payload\b", re.IGNORECASE),
    re.compile(r"\b(?:full[_ -]?prompt|tool[_ -]?log)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"(?:^|\s)/(?:Users|private|tmp|var/folders)/\S+", re.IGNORECASE),
)

FIELD_LABELS = {
    "action": "action",
    "classification": "action",
    "conviction": "conviction",
    "catalyst": "catalyst",
    "catalyst date": "catalyst_date",
    "bull case": "bull_case",
    "bear case": "bear_case",
    "entry zone": "entry_zone",
    "maximum acceptable entry": "maximum_acceptable_entry",
    "max acceptable entry": "maximum_acceptable_entry",
    "stop": "stop_or_invalidation",
    "thesis invalidation": "stop_or_invalidation",
    "stop or thesis invalidation": "stop_or_invalidation",
    "target 1": "target_1",
    "target 2": "target_2",
    "position size": "position_size_or_risk_budget",
    "risk budget": "position_size_or_risk_budget",
    "verified position size or risk budget": "position_size_or_risk_budget",
    "holding period": "holding_period",
    "expected holding period": "holding_period",
    "risk/reward": "risk_reward",
    "liquidity": "liquidity_risk",
    "spread": "spread_risk",
    "volatility": "volatility_risk",
    "dilution": "dilution_risk",
    "gap": "gap_risk",
    "concentration": "concentration_risk",
    "correlation": "correlation_risk",
    "kalshi": "kalshi",
    "facts": "facts",
    "verified facts": "facts",
    "assumptions": "assumptions",
    "speculation": "speculation",
}

ACTION_LABELS = {
    "BUY": "Buy",
    "WATCH": "Watch",
    "AVOID": "Avoid",
    "HOLD": "Hold",
    "REDUCE": "Reduce",
    "EXIT": "Exit",
    "NO TRADE": "No Trade",
}


def _safe_reason(code: str) -> str:
    token = re.sub(r"[^a-z0-9_.:-]+", "_", str(code).lower()).strip("_")
    return token[:160] or "report_unavailable"


def _validate_target(target: str) -> None:
    if not TARGET_RE.fullmatch(target):
        raise SourceError("target_date_invalid")
    try:
        if dt.date.fromisoformat(target).isoformat() != target:
            raise ValueError
    except ValueError as exc:
        raise SourceError("target_date_invalid") from exc


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _regular_single_link(path: Path, parent: Path) -> bool:
    try:
        info = path.lstat()
        return (not path.is_symlink() and path.is_file() and info.st_nlink == 1
                and _inside(path, parent))
    except OSError:
        return False


def _validate_directory(directory: Path, approved_root: Path) -> None:
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir() or not _inside(directory, approved_root):
        raise SourceError("report_directory_boundary_invalid")


def _has_casefold_collision(names: list[str]) -> bool:
    folded = [name.casefold() for name in names]
    return len(folded) != len(set(folded))


def _guard_precedence(root: Path, target: str) -> None:
    """Reject case-fold aliases and same-stamp Preliminary ties before selection."""
    reports_root = root / "reports"
    morning = reports_root / "morning"
    preliminary = morning / "preliminary"
    official = morning / "official_research"
    for directory in (morning, preliminary, official):
        _validate_directory(directory, reports_root)

    for directory in (morning, preliminary, official):
        if not directory.is_dir():
            continue
        names: list[str] = []
        for entry in directory.iterdir():
            if target not in entry.name:
                continue
            try:
                if entry.is_symlink() or not entry.is_file() or entry.lstat().st_nlink != 1:
                    raise SourceError("report_candidate_boundary_invalid")
            except OSError as exc:
                raise SourceError("report_candidate_boundary_invalid") from exc
            names.append(entry.name)
        if _has_casefold_collision(names):
            raise SourceError("report_casefold_collision")

    if preliminary.is_dir():
        stamps: dict[str, int] = {}
        for entry in preliminary.iterdir():
            match = PRELIM_RE.fullmatch(entry.name)
            if match and match.group("date") == target:
                stamps[match.group("hhmm")] = stamps.get(match.group("hhmm"), 0) + 1
        if any(count > 1 for count in stamps.values()):
            raise SourceError("ambiguous_preliminary_precedence")


def _validate_loaded_source(root: Path, document: SourceDocument) -> None:
    if document.path is None:
        return
    reports_root = root / "reports" / "morning"
    if not _regular_single_link(document.path, reports_root):
        raise SourceError("report_source_boundary_invalid")
    before = document.path.stat()
    actual = sha256_file(document.path)
    after = document.path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or actual != document.sha256:
        raise SourceError("report_source_changed_during_read")

    if document.kind == "official_research":
        receipt_path = root / "data" / "runtime" / "research" / document.target_date / "freeze_receipt.json"
        if not _regular_single_link(receipt_path, root / "data" / "runtime" / "research"):
            raise SourceError("official_receipt_boundary_invalid")
        result = freeze_completion_probe.probe(
            document.target_date,
            root / "data" / "runtime" / "research" / document.target_date,
            root / "reports",
            official_only=True,
        )
        if (result.get("completed") is not True or result.get("official") is not True
                or result.get("classification") != document.classification):
            raise SourceError("official_finality_chain_invalid:" + _safe_reason(result.get("reason", "invalid")))
        if ((document.classification == "OFFICIAL_RESEARCH_BUY_PROPOSAL")
                != (document.buy_authorized is True)):
            raise SourceError("official_buy_authority_binding_invalid")
    elif document.kind == "legacy_official":
        receipt_path = legacy_morning_receipt.receipt_path(document.target_date, root)
        if not _regular_single_link(receipt_path, root / "data" / "runtime" / "research"):
            raise SourceError("legacy_receipt_boundary_invalid")
        errors = legacy_morning_receipt.validate_receipt(
            document.receipt, document.target_date, root)
        if errors:
            raise SourceError("legacy_finality_chain_invalid")


def _section_text(document: SourceDocument, *names: str) -> str | None:
    wanted = {name.casefold() for name in names}
    for section in document.sections:
        if section.heading.strip().casefold() in wanted and section.body.strip():
            return section.body.strip()
    return None


def _extract_symbols(document: SourceDocument) -> list[str]:
    """Lift symbols from authoritative candidate headings in source order; never sort."""
    body = _section_text(document, "Best Actionable Setups", "Best Market Setup")
    if not body or body.strip().casefold().startswith(("none", "not applicable", "无", "n/a")):
        return []
    symbols: list[str] = []
    for line in body.splitlines():
        match = TICKER_RE.match(line)
        if match and match.group(1) not in symbols:
            symbols.append(match.group(1))
    return symbols


def _contains_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def _plain(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*", r"\1", text)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        line = _plain(line.strip(" -*>#\t"))
        if line:
            return line
    return None


def _action(document: SourceDocument) -> str | None:
    if document.status == STATUS_REPORT_FAILURE:
        return None
    decision = _first_line(_section_text(document, "Final Decision"))
    if not decision:
        return "No Trade" if document.status in (
            STATUS_PRELIMINARY_NO_BUY, STATUS_OFFICIAL_NO_TRADE) else None
    upper = decision.upper()
    for token in ("NO TRADE", "REDUCE", "AVOID", "WATCH", "HOLD", "EXIT", "BUY"):
        if re.search(rf"(?:^|[^A-Z]){re.escape(token)}(?:[^A-Z]|$)", upper):
            if token == "BUY" and not document.buy_authorized:
                return None
            return ACTION_LABELS[token]
    return None


def _candidate_blocks(document: SourceDocument) -> list[dict[str, Any]]:
    body = _section_text(document, "Best Actionable Setups", "Best Market Setup")
    symbols = _extract_symbols(document)
    if not body or not symbols:
        return []
    boundaries: list[tuple[int, str, str | None]] = []
    lines = body.splitlines()
    heading_re = re.compile(
        r"^\s*(?:#{2,4}\s*)?(?:\d+[.)]\s*)?\*{0,2}([A-Z]{1,5})\*{0,2}\s*(?:[—–-]|\|)\s*(.+?)\s*$")
    for index, line in enumerate(lines):
        match = heading_re.match(line)
        if match and match.group(1) in symbols:
            company = _plain(match.group(2)).strip(" *") or None
            boundaries.append((index, match.group(1), company))
    if [symbol for _, symbol, _ in boundaries] != symbols:
        # Preserve the exact authoritative symbol order even if the prose lacks parseable blocks.
        return [{"symbol": symbol, "company": None, "fields": {}, "conflicting_fields": []}
                for symbol in symbols]

    candidates: list[dict[str, Any]] = []
    for position, (start, symbol, company) in enumerate(boundaries):
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
        fields: dict[str, str] = {}
        conflicts: list[str] = []
        for line in lines[start + 1:end]:
            match = re.match(
                r"^\s*[-*]?\s*\*{0,2}([^:*]+?)(?::\*{0,2}|\*{0,2}\s*:)\s*(.+?)\s*$",
                line)
            if not match:
                continue
            public_key = FIELD_LABELS.get(_plain(match.group(1)).casefold())
            value = _plain(match.group(2))
            if public_key and value:
                if public_key in fields and fields[public_key] != value:
                    fields[public_key] = "未验证（冲突）"
                    if public_key not in conflicts:
                        conflicts.append(public_key)
                elif public_key not in fields:
                    fields[public_key] = value
        candidates.append({"symbol": symbol, "company": company, "fields": fields,
                           "conflicting_fields": conflicts})
    return candidates


def _public_sections(document: SourceDocument) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for section in document.sections:
        heading = section.heading.strip()
        if heading.casefold() in PUBLIC_SECTIONS and section.body.strip():
            result.append({"heading": heading, "body": section.body.strip()})
    return result


def _not_ready(target: str) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "target_session_date": target,
        "status": STATUS_NOT_READY,
        "title": "Morning report not ready",
        "kind": "none",
        "classification": None,
        "generated_utc": None,
        "data_cutoff": None,
        "final_action": None,
        "official_research": False,
        "buy_authorized": False,
        "execution_enabled": False,
        "options_execution_enabled": False,
        "live_price_authority": None,
        "market_regime": None,
        "candidate_count": 0,
        "top_symbols": [],
        "candidates": [],
        "main_blocker": "no_authoritative_report_artifact",
        "sections": [],
        "unverified_fields": [],
        "audit": {"source_kind": "none", "source_name": None, "source_sha256": None,
                  "receipt_sha256": None},
        "safety": dict(SAFETY),
    }


def _failure(target: str, reason: str) -> dict[str, Any]:
    document = failure_document(target, _safe_reason(reason))
    return _view(document)


def _engineering_status(root: Path, target: str) -> dict[str, Any]:
    try:
        return morning_audit_supervisor.console_status(Path(root), target)
    except Exception:
        # Engineering is a separate read-only layer. Its failure must never suppress or mutate
        # the independently validated market-report projection.
        return {
            "state": "FAILED", "health": "UNKNOWN", "reliability_score": None,
            "breakdown": {}, "root_cause_or_evidence_gap": "工程审计读取失败；市场报告未受影响。",
            "warnings": ["audit_read_failed"], "manual_actions": [],
            "execution_enabled": False, "options_execution_enabled": False,
        }


def _view(document: SourceDocument) -> dict[str, Any]:
    authority_headings = {
        "best actionable setups", "best market setup", "final decision", "buy block reasons"}
    heading_counts: dict[str, int] = {}
    for section in document.sections:
        key = section.heading.strip().casefold()
        if key in authority_headings:
            heading_counts[key] = heading_counts.get(key, 0) + 1
    if any(count > 1 for count in heading_counts.values()):
        return _failure(document.target_date, "duplicate_authoritative_section")
    sections = _public_sections(document)
    aggregate = "\n".join(
        [document.title, document.failure_reason or ""]
        + [item["heading"] + "\n" + item["body"] for item in sections])
    if _contains_sensitive(aggregate):
        if document.status == STATUS_REPORT_FAILURE and document.failure_reason == "sensitive_content_blocked":
            sections = []
        else:
            return _failure(document.target_date, "sensitive_content_blocked")

    candidates = _candidate_blocks(document) if sections else []
    if not document.buy_authorized and any(
            str(row.get("fields", {}).get("action", "")).strip().casefold() == "buy"
            for row in candidates):
        return _failure(document.target_date, "unauthorized_buy_claim_blocked")
    decision_line = _first_line(_section_text(document, "Final Decision")) or ""
    if not document.buy_authorized and re.match(r"^BUY(?:[^A-Z]|$)", decision_line.upper()):
        return _failure(document.target_date, "unauthorized_buy_claim_blocked")
    symbols = [row["symbol"] for row in candidates]
    supported_symbols = _extract_symbols(document) if sections else []
    if symbols != supported_symbols:
        return _failure(document.target_date, "candidate_order_validation_failed")

    market_regime = _first_line(_section_text(document, "Market Regime"))
    blocker = document.failure_reason
    if not blocker and document.buy_block_reasons:
        blocker = document.buy_block_reasons[0]
    if not blocker:
        blocker = _first_line(_section_text(document, "Portfolio Risk Alerts", "Data Quality"))

    missing: list[str] = []
    for name, value in (
            ("generated_utc", document.generated_utc),
            ("data_cutoff", document.data_cutoff),
            ("market_regime", market_regime),
            ("live_price_authority", document.live_price_authority)):
        if value is None:
            missing.append(name)

    return {
        "schema": REPORT_SCHEMA,
        "target_session_date": document.target_date,
        "status": STATUS_LABELS.get(document.status, STATUS_REPORT_FAILURE),
        "title": document.title or "Morning Report",
        "kind": document.kind,
        "classification": document.classification,
        "generated_utc": document.generated_utc,
        "data_cutoff": document.data_cutoff,
        "final_action": _action(document),
        "official_research": document.kind == "official_research" and document.is_official,
        "buy_authorized": document.buy_authorized is True,
        "execution_enabled": False,
        "options_execution_enabled": False,
        "live_price_authority": document.live_price_authority,
        "market_regime": market_regime,
        "candidate_count": len(candidates),
        "top_symbols": symbols,
        "candidates": candidates,
        "main_blocker": blocker,
        "sections": sections,
        "unverified_fields": missing,
        "audit": {
            "source_kind": document.kind,
            "source_name": document.path.name if document.path else None,
            "source_sha256": document.sha256,
            "receipt_sha256": document.receipt_sha256,
        },
        "safety": dict(SAFETY),
    }


def report_for_date(root: Path, target: str) -> dict[str, Any]:
    """Return one safe view. Validation errors become a factual failure, never a fallback."""
    try:
        _validate_target(target)
        root = Path(root).resolve(strict=True)
        _guard_precedence(root, target)
        document = load_source(root, target)
        if document.kind == "none":
            result = _not_ready(target)
        else:
            _validate_loaded_source(root, document)
            result = _view(document)
    except SourceError as exc:
        result = _failure(target if TARGET_RE.fullmatch(target) else "0000-00-00", str(exc))
    except (OSError, ValueError):
        result = _failure(target if TARGET_RE.fullmatch(target) else "0000-00-00",
                          "report_source_unavailable")
    result["engineering_audit"] = (_engineering_status(Path(root), target)
                                   if TARGET_RE.fullmatch(target) else {
                                       "state": "FAILED", "health": "UNKNOWN",
                                       "execution_enabled": False,
                                       "options_execution_enabled": False,
                                   })
    return result


def available_dates(root: Path) -> list[str]:
    """Enumerate date tokens only from the three fixed morning-report roots."""
    root = Path(root).resolve(strict=True)
    reports_root = root / "reports"
    morning = reports_root / "morning"
    directories = (morning, morning / "official_research", morning / "preliminary")
    dates: set[str] = set()
    for directory in directories:
        _validate_directory(directory, reports_root)
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.lstat().st_nlink != 1:
                continue
            match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:_|\.md$)", entry.name)
            if match:
                try:
                    _validate_target(match.group(1))
                except SourceError:
                    continue
                dates.add(match.group(1))
    return sorted(dates, reverse=True)


def latest_report(root: Path, target_today: str) -> dict[str, Any]:
    """The home/latest surface is for today's target; an older file never impersonates today."""
    try:
        _validate_target(target_today)
        if target_today not in available_dates(root):
            result = _not_ready(target_today)
            result["engineering_audit"] = _engineering_status(Path(root), target_today)
            return result
        return report_for_date(root, target_today)
    except (SourceError, OSError, ValueError):
        result = _failure(target_today, "latest_report_source_unavailable")
        result["engineering_audit"] = _engineering_status(Path(root), target_today)
        return result


def report_history(root: Path, limit: int) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30:
        raise SourceError("history_limit_invalid")
    rows: list[dict[str, Any]] = []
    for target in available_dates(root)[:limit]:
        report = report_for_date(root, target)
        rows.append({
            "target_session_date": target,
            "status": report["status"],
            "final_action": report["final_action"],
            "official_research": report["official_research"],
            "buy_authorized": report["buy_authorized"],
            "candidate_count": report["candidate_count"],
            "generated_utc": report["generated_utc"],
            "main_blocker": report["main_blocker"],
            "engineering_audit_state": report["engineering_audit"]["state"],
            "engineering_health": report["engineering_audit"]["health"],
            "reliability_score": report["engineering_audit"].get("reliability_score"),
        })
    return {"schema": HISTORY_SCHEMA, "reports": rows, "execution_enabled": False,
            "options_execution_enabled": False, "safety": dict(SAFETY)}
