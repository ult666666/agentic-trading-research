#!/usr/bin/env python3
"""Authoritative frozen-report source selection for local console rendering.

THE ONE RULE
------------
The publisher RENDERS an already-frozen morning decision. It may not add, remove,
rerank, or newly recommend a stock, and it may never promote a Preliminary artifact to
Official or synthesize Buy authorization. Everything this module returns is either
(a) verbatim bytes from the frozen report, or (b) a deterministic field lifted from the
freeze receipt. There is no third category, and in particular no model prose.

AUTHORITY ORDER (highest first), per governance v2.0 and CLAUDE.md 3a/3b
-----------------------------------------------------------------------
  1. OFFICIAL RESEARCH  reports/morning/official_research/<target>.md
     Honored ONLY when data/runtime/research/<target>/freeze_receipt.json exists, carries
     schema research_freeze_receipt/1.0, names this target, says official=true, and its
     recorded report_sha256 equals the file's actual bytes. A file in that slot is NEVER
     honored from existence alone (mirrors research_freeze.py's re-anchoring).
  2. LEGACY V1 OFFICIAL  reports/morning/<target>.md
     Read-only historical audit surface. New v1 writes are disabled by the permanent
     source-proof overlay, so this is accepted only when a legacy receipt validates it.
     Absent that binding it is IGNORED, never downgraded-but-used.
  3. PRELIMINARY  reports/morning/preliminary/<target>_<label>_<HHMM>PT.md
     The latest by HHMM. Always renders as PRELIMINARY - NO BUY. A Preliminary carries no
     freeze receipt by construction, so none is required — but it must self-identify as
     Preliminary and must not claim Official status or an authorized Buy.
  4. Nothing publishable -> REPORT FAILURE.

A higher tier that exists but fails validation is a HARD ERROR (SourceError), never a
silent fall-through to a lower tier. Falling through would let a corrupted or tampered
Official slot quietly publish yesterday's Preliminary under today's date.

Std-lib only. Read-only: this module opens no network, mutates nothing, and imports no
broker, order, account, or research-execution surface.
"""
from __future__ import annotations

import hashlib
import json
import re

import legacy_morning_receipt
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "morning_report_publication_source/1.0"
RECEIPT_SCHEMA = "research_freeze_receipt/1.0"

#: Display statuses. These five strings are the entire public vocabulary.
STATUS_OFFICIAL = "OFFICIAL"
STATUS_OFFICIAL_NO_TRADE = "OFFICIAL - NO TRADE"
STATUS_PRELIMINARY_NO_BUY = "PRELIMINARY - NO BUY"
STATUS_REPORT_FAILURE = "REPORT FAILURE"
STATUS_TEST = "TEST - NO LIVE DATA"

ALL_STATUSES = (
    STATUS_OFFICIAL, STATUS_OFFICIAL_NO_TRADE, STATUS_PRELIMINARY_NO_BUY,
    STATUS_REPORT_FAILURE, STATUS_TEST,
)

#: Classification -> display status. Anything unknown fails closed to REPORT FAILURE
#: rather than being guessed into an Official-looking bucket.
_CLASSIFICATION_STATUS = {
    "OFFICIAL_RESEARCH_BUY_PROPOSAL": STATUS_OFFICIAL,
    "OFFICIAL_RESEARCH_WATCH": STATUS_OFFICIAL,
    "OFFICIAL_RESEARCH_CONDITIONAL_PLAN": STATUS_OFFICIAL,
    "OFFICIAL_RESEARCH_NO_TRADE": STATUS_OFFICIAL_NO_TRADE,
}

_TARGET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRELIM_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<label>.+)_(?P<hhmm>\d{4})PT\.md$")
_UNVERIFIED = "未验证"
MAX_SOURCE_BYTES = 196608


class SourceError(RuntimeError):
    """A higher-authority artifact exists but does not validate. Fail closed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_read_text(path: Path) -> tuple[str, str]:
    """Read one single-link regular file and bind the returned text to its exact hash."""
    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file() or before.st_nlink != 1:
            raise SourceError("report source is not a single-link regular file")
        if before.st_size > MAX_SOURCE_BYTES:
            raise SourceError("report source exceeds the bounded console limit")
        blob = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise SourceError("report source is unreadable") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(blob) != after.st_size:
        raise SourceError("report source changed during read")
    try:
        text = blob.decode("utf-8")
    except UnicodeError as exc:
        raise SourceError("report source is not UTF-8") from exc
    return text, hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Section:
    """One verbatim '## ' section of the frozen report."""
    heading: str
    body: str


@dataclass
class SourceDocument:
    """The frozen decision, plus only those facts a receipt or the bytes actually support."""
    kind: str                     # official_research | legacy_official | preliminary | none
    status: str                   # one of ALL_STATUSES
    target_date: str
    path: Path | None = None
    sha256: str | None = None
    classification: str | None = None
    receipt: dict | None = None
    receipt_sha256: str | None = None
    sections: list[Section] = field(default_factory=list)
    title: str = ""
    generated_utc: str | None = None
    data_cutoff: str | None = None
    live_price_authority: str | None = None
    buy_authorized: bool = False
    buy_block_reasons: list[str] = field(default_factory=list)
    execution_enabled: bool = False
    failure_reason: str | None = None

    # -- derived, never invented ------------------------------------------------
    @property
    def is_official(self) -> bool:
        return self.status in (STATUS_OFFICIAL, STATUS_OFFICIAL_NO_TRADE)

    @property
    def is_publishable(self) -> bool:
        """A REPORT FAILURE page is still published — it is the day's honest answer."""
        return self.status in ALL_STATUSES

    def display(self, value: object) -> str:
        """Render a value, or the unverified marker. Never fabricates a substitute."""
        if value is None or value == "" or value == []:
            return _UNVERIFIED
        return str(value)

    def idempotency_fields(self) -> tuple[str, str]:
        return self.target_date, (self.sha256 or "no-source")


# ------------------------------------------------------------------- parsing --
def split_sections(text: str) -> tuple[str, list[Section]]:
    """Split a frozen report into (title, ordered '## ' sections). Verbatim, no reflow.

    Content ABOVE the first '## ' (the deterministic downgrade notice, the stamp block)
    is kept as the preamble section so nothing in the frozen file is silently dropped.
    """
    lines = text.splitlines()
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    sections: list[Section] = []
    heading = "__preamble__"
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            body = "\n".join(buffer).strip()
            if body:
                sections.append(Section(heading=heading, body=body))
            heading = line[3:].strip()
            buffer = []
            continue
        if line.startswith("# "):
            continue
        buffer.append(line)
    body = "\n".join(buffer).strip()
    if body:
        sections.append(Section(heading=heading, body=body))
    return title, sections


def _first_match(text: str, pattern: str) -> str | None:
    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1).strip() if found else None


def _bullet_list(sections: list[Section], heading: str) -> list[str]:
    for section in sections:
        if section.heading.lower() == heading.lower():
            return [line.lstrip("-* ").strip()
                    for line in section.body.splitlines() if line.strip().startswith(("-", "*"))]
    return []


def _claims_official(text: str) -> bool:
    """Does this artifact assert Official status or an authorized Buy?"""
    haystack = text.upper()
    if re.search(r"\bOFFICIAL_RESEARCH_(WATCH|NO_TRADE|CONDITIONAL_PLAN|BUY_PROPOSAL)\b", haystack):
        return True
    if "BUY AUTHORIZATION: GRANTED" in haystack or "BUY AUTHORIZED: TRUE" in haystack:
        return True
    return False


# ------------------------------------------------------------------ loaders --
def _load_receipt(root: Path, target: str) -> tuple[dict | None, str | None]:
    path = root / "data" / "runtime" / "research" / target / "freeze_receipt.json"
    if not path.is_file() or path.is_symlink():
        return None, None
    try:
        receipt_text, receipt_sha = stable_read_text(path)
        document = json.loads(receipt_text)
    except (OSError, ValueError, SourceError) as exc:
        raise SourceError(f"freeze receipt for {target} is unreadable") from exc
    if not isinstance(document, dict) or document.get("schema") != RECEIPT_SCHEMA:
        raise SourceError(f"freeze receipt for {target} carries an unknown schema")
    if document.get("target_session_date") != target:
        raise SourceError(
            f"freeze receipt names {document.get('target_session_date')!r}, not {target!r}")
    return document, receipt_sha


def _official_research(root: Path, target: str) -> SourceDocument | None:
    path = root / "reports" / "morning" / "official_research" / f"{target}.md"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SourceError("official-research slot is not a regular file")

    receipt, receipt_sha = _load_receipt(root, target)
    if receipt is None:
        raise SourceError(
            f"{path.name} occupies the official-research slot with no freeze receipt; "
            "an official slot is never honored from existence alone")
    if receipt.get("official") is not True:
        raise SourceError("freeze receipt does not record an official publication")

    text, actual = stable_read_text(path)
    if actual != receipt.get("report_sha256"):
        raise SourceError(
            "official-research report bytes do not match the freeze receipt "
            f"({actual[:12]} vs {str(receipt.get('report_sha256'))[:12]})")

    classification = str(receipt.get("classification") or "")
    status = _CLASSIFICATION_STATUS.get(classification)
    if status is None:
        raise SourceError(f"unknown official classification {classification!r}")

    title, sections = split_sections(text)
    return SourceDocument(
        kind="official_research",
        status=status,
        target_date=target,
        path=path,
        sha256=actual,
        classification=classification,
        receipt=receipt,
        receipt_sha256=receipt_sha,
        sections=sections,
        title=title,
        generated_utc=str(receipt.get("frozen_utc") or "") or None,
        data_cutoff=_first_match(text, r"^[-*]?\s*Data cutoff:\s*(.+)$"),
        live_price_authority=str(receipt.get("live_price_authority") or "") or None,
        buy_authorized=receipt.get("buy_authorized") is True,
        buy_block_reasons=[str(x) for x in (receipt.get("buy_block_reasons") or [])],
        execution_enabled=False,
    )


def _legacy_official(root: Path, target: str) -> SourceDocument | None:
    """Historical v1 slot, honored only through its deterministic legacy validator."""
    path = root / "reports" / "morning" / f"{target}.md"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        raise SourceError("legacy official slot is not a single-link regular file")

    receipt_path = legacy_morning_receipt.receipt_path(target, root)
    if (not receipt_path.is_file() or receipt_path.is_symlink()
            or receipt_path.lstat().st_nlink != 1):
        return None
    try:
        receipt_text, receipt_sha = stable_read_text(receipt_path)
        receipt = json.loads(receipt_text)
    except (OSError, ValueError) as exc:
        raise SourceError(f"legacy receipt for {target} is unreadable") from exc
    if legacy_morning_receipt.validate_receipt(receipt, target, root):
        raise SourceError("legacy receipt does not revalidate")

    text, actual = stable_read_text(path)
    title, sections = split_sections(text)
    decision = _first_match(text, r"^\*{0,2}(NO TRADE)[.!* ]")
    return SourceDocument(
        kind="legacy_official",
        status=STATUS_OFFICIAL_NO_TRADE if decision else STATUS_OFFICIAL,
        target_date=target,
        path=path,
        sha256=actual,
        classification="OFFICIAL_MORNING",
        receipt=receipt,
        receipt_sha256=receipt_sha,
        sections=sections,
        title=title,
        generated_utc=str(receipt.get("created_utc") or "") or None,
        data_cutoff=_first_match(text, r"^[-*]?\s*Data cutoff:\s*(.+)$"),
        live_price_authority=None,
        buy_authorized=False,
        buy_block_reasons=["legacy_v1_slot_is_audit_only"],
        execution_enabled=False,
    )


def _preliminary(root: Path, target: str) -> SourceDocument | None:
    directory = root / "reports" / "morning" / "preliminary"
    if not directory.is_dir():
        return None

    candidates: list[tuple[str, Path, str]] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        found = _PRELIM_NAME_RE.match(entry.name)
        if found and found.group("date") == target:
            candidates.append((found.group("hhmm"), entry, found.group("label")))
    if not candidates:
        return None

    # Latest stamp wins. This is a within-tier tie-break on the SAME authority level; it
    # can never raise a Preliminary above a validated Official.
    hhmm, path, label = sorted(candidates)[-1]
    text, source_sha = stable_read_text(path)
    if _claims_official(text):
        raise SourceError(
            f"{path.name} sits in the preliminary tree but asserts Official status or an "
            "authorized Buy; refusing to publish a self-contradicting artifact")

    title, sections = split_sections(text)
    classification = _first_match(text, r"^[-*]?\s*Classification:\s*\*{0,2}(\S+?)\*{0,2}\s*$")
    return SourceDocument(
        kind="preliminary",
        status=STATUS_PRELIMINARY_NO_BUY,
        target_date=target,
        path=path,
        sha256=source_sha,
        classification=classification or f"PRELIMINARY_{label.upper()}",
        receipt=None,
        receipt_sha256=None,
        sections=sections,
        title=title,
        generated_utc=_first_match(text, r"^[-*]?\s*Generated \(UTC\):\s*(.+)$"),
        data_cutoff=_first_match(text, r"^[-*]?\s*Data cutoff:\s*(.+)$"),
        live_price_authority=None,          # a Preliminary asserts no live-price authority
        buy_authorized=False,               # structurally impossible in this tier
        buy_block_reasons=_bullet_list(sections, "Buy Block Reasons") or ["preliminary_stage"],
        execution_enabled=False,
    )


def load_source(root: Path, target: str) -> SourceDocument:
    """Resolve the highest-authority frozen artifact for `target`.

    Raises SourceError when a higher tier exists but fails validation. Returns a
    REPORT FAILURE document when nothing publishable exists at all.
    """
    root = Path(root)
    if not _TARGET_RE.match(target):
        raise SourceError(f"target session date {target!r} is not YYYY-MM-DD")

    for loader in (_official_research, _legacy_official, _preliminary):
        document = loader(root, target)
        if document is not None:
            return document

    return SourceDocument(
        kind="none",
        status=STATUS_REPORT_FAILURE,
        target_date=target,
        failure_reason=(
            "No frozen morning artifact exists for this session: the run produced no "
            "official-research report and no preliminary checkpoint."),
        buy_block_reasons=["no_frozen_artifact"],
    )


def failure_document(target: str, reason: str) -> SourceDocument:
    """A REPORT FAILURE document carrying a receipt-derived reason, never a guess."""
    return SourceDocument(
        kind="none",
        status=STATUS_REPORT_FAILURE,
        target_date=target,
        failure_reason=reason,
        buy_block_reasons=["report_failure"],
    )
