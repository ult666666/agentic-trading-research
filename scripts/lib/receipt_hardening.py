#!/usr/bin/env python3
"""Receipt-bridge hardening primitives (2026-07-23) — create-once writes, sealed receipt
sets, and claim-semantics binding for the catalyst receipt lanes.

WHY (the 2026-07-21 deregistration, journal §6032-6058): the provisional SEC/news receipt
validators "authenticated retrieval structure but did not bind exact claim semantics,
same-run provenance, immutable receipt sets, or independent source roles." This module
supplies the deterministic primitives for three of those four axes:

  * IMMUTABLE RECEIPT SETS — create_once_write_json refuses to overwrite any receipt or
    collection summary (O_EXCL); build_set_seal inventories the exact receipt files of one
    collection pass with per-file sha256 + a set hash + the audited run id;
    verify_member_of_set denies any receipt that is missing from, altered since, or
    orphaned outside its sealed collection set.
  * EXACT CLAIM SEMANTICS — extract_semantics derives bounded, non-raw semantic features
    (keyword classes, dates, amounts) from anchored fragment BYTES at collection time;
    claim_semantics_check later denies a claim whose catalyst_type/value is not actually
    evidenced by the anchored fragment (the "any claim can point at any anchor" hole).
  * SAME-RUN PROVENANCE (recorded only, deliberately NOT verified) — the set seal
    records TRADING_BROKER_AUDIT_RUN_ID as audit context. Verification of it is
    intentionally absent, for two adversarially-confirmed reasons (2026-07-23 review):
    (1) the audited morning model child INHERITS that env var (audited_claude.py builds
    the child env from os.environ + the run id), so an env-based check provides zero
    protection against the in-scope prompt-injection adversary; (2) the runner mints the
    id only at broker-audit time, AFTER the collectors have already sealed, so any
    verification would deny every honest morning. Real binding requires the WI-2
    child-invisible run-context (see NOTES_NEXT.md).

Raw document text is never persisted: extraction emits only bounded canonical values
(ISO dates, canonical dollar/percent strings, class tokens). Everything here is pure and
offline; no network, no model, no broker surface; execution_enabled remains false
everywhere. This module grants nothing by itself — the production validator registry
(scripts/evidence_schema.py) stays empty until the separate, user-ratified activation
ceremony; validators consult these primitives inertly until then.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SEMANTICS_VERSION = "1.0.0"
SET_SEAL_SCHEMA = "receipt_set_seal/1.0"
AUDIT_RUN_ENV = "TRADING_BROKER_AUDIT_RUN_ID"
_MAX_LIST = 64
_MAX_FRAGMENT_BYTES = 8_388_608

# Catalyst-type -> keyword class table (versioned with SEMANTICS_VERSION). Every
# catalyst_type token the collectors can permit (sec_catalyst_receipt_collector
# EIGHT_K_ITEM_TYPES / FORM_TYPES / offering prefix) MUST have an entry here; an unmapped
# type fails closed in claim_semantics_check. Item-section fragments begin with their
# canonical SEC heading text, so the correct section always evidences its own class while
# a wrong-section pointer does not.
KEYWORD_CLASSES: dict[str, tuple[str, ...]] = {
    "earnings": ("earnings", "results of operations", "financial results", "quarter",
                 "fiscal year", "revenue", "net income", "guidance"),
    "periodic_results": ("earnings", "results of operations", "financial results",
                         "quarter", "fiscal year", "revenue", "net income",
                         "annual report", "quarterly report"),
    "merger_acquisition": ("merger", "acquisition", "acquire", "business combination",
                           "purchase agreement", "tender offer", "completion of"),
    "asset_disposition": ("disposition", "divestiture", "sale of assets",
                          "asset purchase", "completion of acquisition or disposition"),
    "material_agreement": ("material definitive agreement", "entry into", "agreement",
                           "contract"),
    "agreement_termination": ("termination of a material definitive agreement",
                              "termination", "terminate"),
    "bankruptcy": ("bankruptcy", "chapter 11", "chapter 7", "receivership"),
    "financing_obligation": ("direct financial obligation", "credit agreement", "notes",
                             "indenture", "loan", "financing"),
    "restructuring": ("restructuring", "exit or disposal", "cost reduction",
                      "plan of termination"),
    "impairment": ("impairment", "write-down", "writedown", "charge"),
    "listing_status": ("notice of delisting", "delisting", "listing", "transfer listing",
                       "continued listing", "nasdaq", "nyse"),
    "auditor_change": ("certifying accountant", "auditor", "accountant"),
    "restatement": ("non-reliance", "restatement", "should no longer be relied"),
    "control_change": ("changes in control", "change in control"),
    "officer_change": ("departure of directors", "officer", "director", "appointment",
                       "resignation", "election of directors"),
    "charter_bylaws_change": ("articles of incorporation", "bylaws", "amendments to"),
    "shareholder_vote": ("submission of matters to a vote", "annual meeting",
                         "special meeting", "stockholders", "shareholders"),
    "regulation_fd": ("regulation fd", "reg fd", "investor presentation",
                      "press release", "furnished"),
    "material_event": ("other events", "material event"),
    "foreign_report": ("report of foreign private issuer", "foreign private issuer",
                       "6-k"),
    "offering_dilution": ("offering", "prospectus", "registration statement",
                          "underwrit", "shares of common stock", "dilution"),
    "stake_activism": ("schedule 13d", "beneficial ownership", "activis"),
    "stake_disclosure": ("schedule 13g", "beneficial ownership"),
    "tender_offer": ("tender offer", "sc to"),
}

_TAG_RE = re.compile(rb"<[^>]{0,512}>")
_WS_RE = re.compile(r"\s+")
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_TEXT_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})\b")
_UNIT_WORDS = r"(million|billion|thousand|trillion|bn|mm|[bmkt])"
_DOLLAR_RE = re.compile(
    r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*" + _UNIT_WORDS + r"?\b")
_PERCENT_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s?%")
_MULTIPLIERS = {"thousand": 1_000, "k": 1_000,
                "million": 1_000_000, "m": 1_000_000, "mm": 1_000_000,
                "billion": 1_000_000_000, "b": 1_000_000_000, "bn": 1_000_000_000,
                "trillion": 1_000_000_000_000, "t": 1_000_000_000_000}
_LOOSE_ISO_RE = re.compile(r"^(20\d{2})-(\d{1,2})-(\d{1,2})$")


# --------------------------------------------------------------------- create-once writes
def create_once_write_json(path: Path, document: dict) -> None:
    """Atomically create `path` with the canonical JSON of `document`. Raises
    FileExistsError if the path already exists — receipts and collection summaries are
    immutable once written (re-collection must never silently rewrite history)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link into place: fails with FileExistsError when the target exists, and
        # never replaces existing bytes (os.replace would).
        os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


# ------------------------------------------------------------------------- set sealing
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_receipt_files(directory: Path) -> list[str]:
    """Every receipt .json currently in `directory` (collection summaries excluded).
    Collectors seal THIS snapshot — the union of what exists at seal time — so a
    same-morning re-run after a mid-run death (the documented rc=124 pattern) seals the
    surviving receipts instead of an empty or partial list."""
    directory = Path(directory)
    try:
        return sorted(p.name for p in directory.iterdir()
                      if p.is_file() and not p.is_symlink() and p.suffix == ".json"
                      and p.name not in _COLLECTION_NAMES)
    except OSError:
        return []


def build_set_seal(directory: Path, filenames: list[str] | None = None) -> dict:
    """Seal the receipt files present at the end of a collection pass: per-file sha256
    of the bytes on disk, a canonical set hash, and the audited run id (recorded for
    audit only — see the module docstring; verification is deliberately absent).
    `filenames=None` seals the directory snapshot via list_receipt_files."""
    directory = Path(directory)
    if filenames is None:
        filenames = list_receipt_files(directory)
    files: dict[str, str] = {}
    errors: list[str] = []
    for name in sorted(set(str(n) for n in filenames)):
        if "/" in name or "\\" in name or name.startswith("."):
            errors.append(f"seal_filename_invalid:{name}")
            continue
        candidate = directory / name
        try:
            files[name] = _sha256_bytes(candidate.read_bytes())
        except OSError:
            errors.append(f"seal_member_unreadable:{name}")
    set_hash = _sha256_bytes(json.dumps({"files": files}, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8"))
    return {
        "schema": SET_SEAL_SCHEMA,
        "receipt_count": len(files),
        "files": files,
        "receipts_set_sha256": set_hash,
        "audited_run_id": os.environ.get(AUDIT_RUN_ENV) or None,
        "seal_errors": sorted(errors),
    }


_COLLECTION_NAMES = ("_collection.json", "_news_collection.json")


def verify_member_of_set(directory: Path, filename: str,
                         collection_name: str = "_collection.json") -> list[str]:
    """Deny-reasons for `filename` against the sealed collection set in `directory`.

    Fail-closed on: missing/invalid seal, non-membership, altered bytes, or an
    inconsistent seal. An empty list means the CLAIMED receipt is a byte-identical
    member of an intact, sealed set.

    Deliberately absent (2026-07-23 adversarial review):
      * NO orphan-poisoning — a stray uninventoried file cannot grant anything (a claim
        citing it fails non-membership right here), so denying OTHER sealed receipts for
        its presence added no security and handed any writer a whole-day availability
        DoS + made crash re-runs self-poisoning.
      * NO audited-run-id verification — the id is recorded in the seal for audit, but
        the morning model child inherits the env var and the runner mints it after
        collection, so checking it would deny honest mornings while stopping no
        attacker. Real same-run binding is WI-2 (child-invisible run-context)."""
    directory = Path(directory)
    try:
        collection = json.loads((directory / collection_name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["collection_summary_unreadable"]
    seal = collection.get("set_seal") if isinstance(collection, dict) else None
    if not isinstance(seal, dict) or seal.get("schema") != SET_SEAL_SCHEMA:
        return ["set_seal_missing_or_invalid"]
    files = seal.get("files")
    if not isinstance(files, dict):
        return ["set_seal_files_invalid"]
    errors: list[str] = []
    if seal.get("seal_errors"):
        errors.append("set_seal_recorded_errors")
    expected_hash = _sha256_bytes(json.dumps({"files": files}, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8"))
    if seal.get("receipts_set_sha256") != expected_hash:
        errors.append("set_hash_mismatch")
    if seal.get("receipt_count") != len(files):
        errors.append("receipt_count_mismatch")
    if filename not in files:
        errors.append("receipt_not_in_sealed_set")
    else:
        try:
            current = _sha256_bytes((directory / filename).read_bytes())
        except OSError:
            current = None
        if current != files[filename]:
            errors.append("receipt_bytes_differ_from_seal")
    return sorted(set(errors))


# ---------------------------------------------------------------- semantic extraction
def _fragment_text(fragment: bytes) -> str:
    if not isinstance(fragment, (bytes, bytearray)):
        return ""
    stripped = _TAG_RE.sub(b" ", bytes(fragment[:_MAX_FRAGMENT_BYTES]))
    # EDGAR HTML pervasively entity-encodes whitespace and symbols (&nbsp; &amp; &#160;)
    # — decode BEFORE tokenizing or "$2.5&nbsp;billion" fails value extraction
    # (2026-07-23 adversarial review, false-deny class).
    text = html.unescape(stripped.decode("latin-1", "replace"))
    text = text.replace("\xa0", " ").lower()
    return _WS_RE.sub(" ", text)


def _canonical_amount(number_text: str, unit: str | None) -> str | None:
    try:
        value = float(number_text.replace(",", ""))
    except ValueError:
        return None
    value *= _MULTIPLIERS.get((unit or "").lower(), 1)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value == int(value):
        return str(int(value))
    return repr(value)


def canonical_claim_amount(value: Any) -> str | None:
    """Canonicalize a claim's normalized_value into the same amount space as
    extract_semantics, or None when the value is not money-like."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _canonical_amount(str(value), None)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    match = re.fullmatch(
        r"\$?\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*" + _UNIT_WORDS + r"?", text)
    if not match:
        return None
    return _canonical_amount(match.group(1), match.group(2))


def extract_semantics(fragment: bytes) -> dict:
    """Bounded, non-raw semantic features of an anchored fragment. Deterministic and
    pure; only canonical values leave this function (never source text)."""
    text = _fragment_text(fragment)
    dates: set[str] = set()
    for year, month, day in _ISO_DATE_RE.findall(text):
        dates.add(f"{year}-{month}-{day}")
    for month_name, day, year in _TEXT_DATE_RE.findall(text):
        month = _MONTHS.index(month_name) + 1
        try:
            dates.add(f"{int(year):04d}-{month:02d}-{int(day):02d}")
        except ValueError:
            continue
    amounts: set[str] = set()
    for number_text, unit in _DOLLAR_RE.findall(text):
        canonical = _canonical_amount(number_text, unit)
        if canonical is not None:
            amounts.add(canonical)
    percents = {f"{p}%" for p in _PERCENT_RE.findall(text)}
    classes = sorted(name for name, keywords in KEYWORD_CLASSES.items()
                     if any(keyword in text for keyword in keywords))
    return {
        "semantics_version": SEMANTICS_VERSION,
        "keyword_classes": classes,
        "dates": sorted(dates)[:_MAX_LIST],
        "amounts": sorted(amounts)[:_MAX_LIST],
        "percents": sorted(percents)[:_MAX_LIST],
    }


def augment_anchor(anchor: dict, body: bytes) -> dict:
    """Return the anchor with a `semantics` block extracted from its own byte range."""
    if not isinstance(anchor, dict):
        return anchor
    try:
        start = int(anchor.get("byte_start"))
        end = int(anchor.get("byte_end"))
    except (TypeError, ValueError):
        return dict(anchor, semantics={"semantics_version": SEMANTICS_VERSION,
                                       "keyword_classes": [], "dates": [],
                                       "amounts": [], "percents": []})
    fragment = bytes(body)[max(0, start):max(0, end)]
    return dict(anchor, semantics=extract_semantics(fragment))


# ------------------------------------------------------------- claim-semantics binding
def _claim_date(value: Any) -> str | None:
    """Canonical padded ISO date for date-like claim values (tolerates unpadded parts so
    '2026-7-3' cannot dodge the date branch), else None."""
    if not isinstance(value, str):
        return None
    match = _LOOSE_ISO_RE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def claim_semantics_check(claim: dict, anchor: dict, filing_date: str | None = None,
                          has_section_anchors: bool = False) -> list[str]:
    """Deny-reasons binding a model-authored claim to the anchored fragment's extracted
    semantics. Fail-closed on: anchors without semantics (pre-hardening receipts),
    version drift, unknown catalyst types, classes/dates/amounts the fragment does not
    evidence, and — when the receipt carries item-section anchors — any attempt to bind
    against the whole-document anchor (anchor shopping: the full document pools every
    section's classes and values, 2026-07-23 adversarial review).

    HONEST SCOPE (best-effort value binding): only two value shapes are bound — ISO
    dates (padded or not) and dollar amounts in `$N[unit]` form (unit words or b/bn/m/
    mm/k/t suffixes). Any other normalized_value (plain text, quarters like 'Q2 2026',
    scientific notation) is NOT value-bound; the class + section + form/item gates still
    apply. This limitation is deliberate and documented rather than silently implied."""
    if not isinstance(claim, dict) or not isinstance(anchor, dict):
        return ["inputs_not_objects"]
    semantics = anchor.get("semantics")
    if not isinstance(semantics, dict):
        return ["anchor_semantics_missing"]
    if semantics.get("semantics_version") != SEMANTICS_VERSION:
        return ["semantics_version_mismatch"]
    errors: list[str] = []
    if has_section_anchors and anchor.get("kind") == "whole_document":
        errors.append("whole_document_anchor_requires_section_binding")
    ctype = str(claim.get("catalyst_type") or "").strip().lower()
    if ctype not in KEYWORD_CLASSES:
        errors.append("catalyst_type_semantics_unmapped")
    elif ctype not in (semantics.get("keyword_classes") or []):
        errors.append("catalyst_class_not_evidenced_in_fragment")
    value = claim.get("normalized_value")
    claimed_date = _claim_date(value)
    if claimed_date is not None:
        evidenced = set(semantics.get("dates") or [])
        if filing_date:
            evidenced.add(str(filing_date))
        if claimed_date not in evidenced:
            errors.append("claim_date_not_evidenced_in_fragment")
    else:
        amount = canonical_claim_amount(value)
        if amount is not None and amount not in (semantics.get("amounts") or []):
            percent_form = (f"{value}".strip()
                            if isinstance(value, str) and f"{value}".strip().endswith("%")
                            else None)
            if percent_form is None or percent_form.lstrip("$ ") not in (
                    semantics.get("percents") or []):
                errors.append("claim_amount_not_evidenced_in_fragment")
    return sorted(set(errors))
