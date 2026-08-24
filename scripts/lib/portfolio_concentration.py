#!/usr/bin/env python3
"""Deterministic cross-broker portfolio concentration diagnostic (research-only, INERT).

WHY: the 2026-07-23 cross-broker recon found the risk layer is structurally BLIND to
factor/sector concentration — `scripts/lib/risk_gate.py` has no sector/correlation logic,
the one `correlation_blocker` hook is permanently False in production, and
`sector_concentration_after` actually measures total invested %, not sector. Only
exact-same-ticker overlap or aggregate exposure can trip. With the Webull individual
account now known to hold a tech/AI-infrastructure-concentrated book, the system needs a
measurement primitive for concentration before any Buy sizing could account for it.

WHAT THIS IS: a pure, offline, std-lib-only function that computes concentration metrics
from a combined-holdings structure. It is wired into NOTHING — no gate, no runner, no
sizing path. It GRANTS NOTHING (diagnostic_only, execution_enabled False forever). Sector/
theme classification is an EXPLICIT INPUT, never invented here: with no classification the
sector dimension is honestly reported "unavailable" and only classification-free metrics
(HHI / effective positions / top-N weights / cross-broker same-name overlap) are computed.

PRIVACY: consumes only {symbol, market_value} rows and cash — never account numbers,
identifiers, or credentials; it refuses any input carrying an account-identifier-like key.

A future, separately-ratified pack may wire this into the risk gate or post-close
observation. This pack installs the primitive only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "cross_broker_concentration/1.0"
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_BROKER_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,31}$")
_FORBIDDEN_KEYS = ("account_number", "account_id", "account", "rhs_account_number",
                   "last_four", "last4", "credential", "token", "user_id", "email")
_MAX_CLASSIFICATION_LABEL_LENGTH = 80
_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
_ACCOUNT_SHAPED_RE = re.compile(
    r"(?i)(?:account[\s_\-]*(?:id|number)|last[\s_\-]*(?:four|4)|"
    r"credential|access[\s_\-]*token|refresh[\s_\-]*token|password|secret)"
)
_LONG_DIGIT_RE = re.compile(r"\d{7,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})"
    r"[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"
)
_PII_LABEL_RE = re.compile(
    r"(?i)(?:social[\s_\-]*security|\bssn\b|date[\s_\-]*of[\s_\-]*birth|"
    r"\bdob\b|(?:home|mailing|street)[\s_\-]*address|\bphone\b|"
    r"(?:customer|client|user)[\s_\-]*(?:id|number)|\bacct\b)"
)

SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "grants_authority": False,
    "feeds_production_scoring": False,
    "account_identifiers_persisted": False,
}


def _round(value: float, places: int = 4) -> float:
    rounded = round(float(value), places)
    return 0.0 if rounded == 0 else rounded


def _decimal(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _scan_forbidden(obj: Any) -> int:
    """Count forbidden keys recursively without reflecting an untrusted key or path."""
    hits = 0
    if isinstance(obj, dict):
        for key, child in obj.items():
            if str(key).strip().lower() in _FORBIDDEN_KEYS:
                hits += 1
            hits += _scan_forbidden(child)
    elif isinstance(obj, list):
        for child in obj:
            hits += _scan_forbidden(child)
    return hits


def _broker_label(raw: Any) -> tuple[str | None, str | None]:
    """Return a bounded display-safe broker alias; never echo a rejected dictionary key."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "broker_label_invalid"
    label = raw.strip()
    if (not _BROKER_LABEL_RE.fullmatch(label) or _EMAIL_RE.search(label)
            or _ACCOUNT_SHAPED_RE.search(label) or _PII_LABEL_RE.search(label)
            or _PHONE_RE.search(label) or _LONG_DIGIT_RE.search(label)):
        return None, "broker_label_unsafe"
    return label, None


def _classification_label(entry: dict, field: str, symbol: str,
                          errors: list[str]) -> str:
    """Return one bounded, non-sensitive external taxonomy label or ``unclassified``.

    Classification is diagnostic input, not a place to persist arbitrary model text.  Invalid
    labels lower the whole analysis to ``ok: false`` while the emitted bucket uses only the safe
    sentinel; the rejected value itself is never copied into output or an error string.
    """
    raw = entry.get(field)
    if raw is None:
        return "unclassified"
    if not isinstance(raw, str):
        errors.append(f"classification_{field}_invalid:{symbol}")
        return "unclassified"
    label = raw.strip()
    if not label:
        return "unclassified"
    if (len(label) > _MAX_CLASSIFICATION_LABEL_LENGTH or not label.isprintable()
            or _EMAIL_RE.search(label) or _ACCOUNT_SHAPED_RE.search(label)
            or _PII_LABEL_RE.search(label) or _PHONE_RE.search(label)
            or _LONG_DIGIT_RE.search(label)):
        errors.append(f"classification_{field}_unsafe:{symbol}")
        return "unclassified"
    return label


def normalize_holdings(brokers: Any) -> tuple[list[dict], list[str]]:
    """Flatten {broker_label: {cash, positions:[{symbol, market_value}]}} into position
    rows tagged with broker. Returns (rows, errors). Fail-closed on malformed rows."""
    rows: list[dict] = []
    errors: list[str] = []
    if not isinstance(brokers, dict) or not brokers:
        return [], ["brokers_not_object"]
    for raw_label, book in brokers.items():
        label, label_error = _broker_label(raw_label)
        if label_error:
            errors.append(label_error)
            continue
        assert label is not None
        if not isinstance(book, dict):
            errors.append(f"broker_book_invalid:{label}")
            continue
        # A missing key is not evidence of an empty portfolio.  Treating a misspelled
        # ``holdings``/``position`` key as ``[]`` previously produced a clean-looking zero
        # concentration result for an unknown book.  An explicit empty list remains valid.
        if "positions" not in book:
            errors.append(f"positions_key_missing:{label}")
            continue
        if not isinstance(book["positions"], list):
            errors.append(f"positions_not_list:{label}")
            continue
        for index, position in enumerate(book["positions"]):
            if not isinstance(position, dict):
                errors.append(f"position_not_object:{label}:{index}")
                continue
            symbol = str(position.get("symbol") or "").strip().upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                errors.append(f"symbol_invalid:{label}:{index}")
                continue
            market_value = _decimal(position.get("market_value"))
            if market_value is None or market_value < 0:
                errors.append(f"market_value_invalid:{label}:{symbol}")
                continue
            rows.append({"broker": label, "symbol": symbol,
                         "market_value": market_value})
    return rows, sorted(set(errors))


def _hhi(weights: list[float]) -> float:
    """Herfindahl-Hirschman Index on fractional weights (sum of squares)."""
    return sum(w * w for w in weights)


def analyze(brokers: Any, *, classification: dict | None = None) -> dict:
    """Compute cross-broker concentration metrics. `classification` maps symbol ->
    {sector, theme}; absent => sector/theme dimension reported unavailable (never
    invented). Pure and deterministic."""
    forbidden_count = _scan_forbidden(brokers) + _scan_forbidden(classification or {})
    if forbidden_count:
        return {"schema": SCHEMA, "ok": False,
                "error": "account_identifier_key_refused",
                "forbidden_keys": ["<redacted>"],
                "forbidden_key_count": forbidden_count, **SAFETY}
    rows, errors = normalize_holdings(brokers)
    if classification is not None and not isinstance(classification, dict):
        errors.append("classification_not_object")
    cash_total = 0.0
    for raw_label, book in (brokers.items() if isinstance(brokers, dict) else []):
        label, label_error = _broker_label(raw_label)
        if label_error or not isinstance(book, dict):
            continue
        assert label is not None
        if "cash" not in book:
            errors.append(f"cash_key_missing:{label}")
            continue
        cash = _decimal(book.get("cash"))
        if cash is None or cash < 0:
            errors.append(f"cash_invalid:{label}")
            continue
        cash_total += cash
    invested = sum(r["market_value"] for r in rows)
    net = invested + cash_total

    # Aggregate a symbol's market value across brokers (cross-broker same-name overlap).
    by_symbol: dict[str, dict] = {}
    for row in rows:
        entry = by_symbol.setdefault(row["symbol"],
                                     {"market_value": 0.0, "brokers": set()})
        entry["market_value"] += row["market_value"]
        entry["brokers"].add(row["broker"])

    positions = []
    for symbol in sorted(by_symbol):
        market_value = by_symbol[symbol]["market_value"]
        positions.append({
            "symbol": symbol,
            "market_value": _round(market_value, 2),
            "weight_of_invested": _round(market_value / invested) if invested > 0 else 0.0,
            "weight_of_net": _round(market_value / net) if net > 0 else 0.0,
            "brokers": sorted(by_symbol[symbol]["brokers"]),
            "cross_broker_same_name": len(by_symbol[symbol]["brokers"]) > 1,
        })
    positions.sort(key=lambda p: (-p["market_value"], p["symbol"]))

    invested_weights = [p["market_value"] / invested for p in positions] if invested > 0 else []
    hhi = _hhi(invested_weights)
    effective_positions = _round(1.0 / hhi, 2) if hhi > 0 else 0.0
    top_n = {}
    running = 0.0
    for rank, position in enumerate(positions, start=1):
        running += position["weight_of_invested"]
        if rank in (1, 3, 5):
            top_n[f"top_{rank}_weight_of_invested"] = _round(running)

    # Sector/theme buckets — ONLY if an explicit classification is supplied.
    sector_block: dict[str, Any] = {"available": False,
                                    "reason": "no_vetted_classification_supplied"}
    if isinstance(classification, dict) and classification:
        unclassified: list[str] = []
        buckets: dict[str, float] = {}
        theme_buckets: dict[str, float] = {}
        bucket_symbols: dict[str, set[str]] = {}
        theme_symbols: dict[str, set[str]] = {}
        for position in positions:
            symbol = position["symbol"]
            raw_entry = classification.get(symbol)
            if raw_entry is None:
                entry = {}
                unclassified.append(symbol)
            elif not isinstance(raw_entry, dict):
                # Classification is an external input.  A malformed row must lower confidence,
                # never crash the diagnostic or silently coerce arbitrary text into a taxonomy.
                entry = {}
                unclassified.append(symbol)
                errors.append(f"classification_entry_invalid:{symbol}")
            else:
                entry = raw_entry
            sector = _classification_label(entry, "sector", symbol, errors)
            theme = _classification_label(entry, "theme", symbol, errors)
            if sector == "unclassified" or theme == "unclassified":
                unclassified.append(symbol)
            buckets[sector] = buckets.get(sector, 0.0) + position["market_value"]
            theme_buckets[theme] = theme_buckets.get(theme, 0.0) + position["market_value"]
            bucket_symbols.setdefault(sector, set()).add(symbol)
            theme_symbols.setdefault(theme, set()).add(symbol)
        sector_rows = sorted(
            ({"sector": s, "market_value": _round(v, 2),
              "symbols": sorted(bucket_symbols[s]),
              "weight_of_invested": _round(v / invested) if invested > 0 else 0.0}
             for s, v in buckets.items()),
            key=lambda r: (-r["market_value"], r["sector"]))
        theme_rows = sorted(
            ({"theme": t, "market_value": _round(v, 2),
              "symbols": sorted(theme_symbols[t]),
              "weight_of_invested": _round(v / invested) if invested > 0 else 0.0}
             for t, v in theme_buckets.items()),
            key=lambda r: (-r["market_value"], r["theme"]))
        sector_block = {
            "available": True,
            "classification_is_external_input": True,
            "unclassified_symbols": sorted(set(unclassified)),
            "sectors": sector_rows,
            "themes": theme_rows,
            "largest_sector_weight_of_invested":
                sector_rows[0]["weight_of_invested"] if sector_rows else 0.0,
            "largest_theme_weight_of_invested":
                theme_rows[0]["weight_of_invested"] if theme_rows else 0.0,
        }

    return {
        "schema": SCHEMA,
        "ok": not errors,
        "input_errors": errors,
        "totals": {
            "net_value": _round(net, 2),
            "invested_value": _round(invested, 2),
            "cash_value": _round(cash_total, 2),
            "cash_weight_of_net": _round(cash_total / net) if net > 0 else 0.0,
            "invested_weight_of_net": _round(invested / net) if net > 0 else 0.0,
            "position_count": len(positions),
            "distinct_broker_count": len({r["broker"] for r in rows}),
        },
        "concentration": {
            "herfindahl_index_of_invested": _round(hhi),
            "effective_number_of_positions": effective_positions,
            "largest_position_weight_of_invested":
                positions[0]["weight_of_invested"] if positions else 0.0,
            **top_n,
            "cross_broker_same_name_symbols":
                [p["symbol"] for p in positions if p["cross_broker_same_name"]],
        },
        "positions": positions,
        "sector_theme": sector_block,
        "note": ("Diagnostic only. Grants no authority and feeds no gate. Sector/theme "
                 "classification is an external input, never invented here. A Buy in a "
                 "name correlated with these holdings is NOT constrained by any current "
                 "gate — this artifact only measures; wiring it into risk is a separate, "
                 "ratified change."),
        **SAFETY,
    }


def validate(document: Any) -> list[str]:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return ["schema_invalid"]
    errors: list[str] = []
    for key, expected in SAFETY.items():
        if document.get(key) is not expected:
            errors.append(f"safety_field_incorrect:{key}")
    for section in ("totals", "concentration", "positions", "sector_theme"):
        if section not in document:
            errors.append(f"missing_section:{section}")
    return sorted(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-broker concentration diagnostic (research-only, inert).")
    parser.add_argument("--holdings", required=True,
                        help="JSON file: {broker_label: {cash, positions:[{symbol,"
                             " market_value}]}}")
    parser.add_argument("--classification",
                        help="optional JSON file: {symbol: {sector, theme}} (external "
                             "input; never invented)")
    args = parser.parse_args(argv)
    try:
        brokers = json.loads(Path(args.holdings).read_text(encoding="utf-8"))
        classification = (json.loads(Path(args.classification).read_text(encoding="utf-8"))
                          if args.classification else None)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": "input_unreadable",
                          "detail": type(exc).__name__, **SAFETY}))
        return 2
    document = analyze(brokers, classification=classification)
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
