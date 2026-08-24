#!/usr/bin/env python3
"""Artifact provenance primitives (std-lib only; research diagnostics support).

Small, deterministic helpers that let the post-close diagnostic chain
(archive summary -> research outcomes -> attribution summary -> pipeline health) prove that
every artifact belongs to ONE target session and ONE runner invocation, and that each stage
consumed exactly the bytes the previous stage produced.

Safety contract:
  * Run IDs are opaque, non-secret, filesystem-safe tokens: a UTC timestamp component (via
    tzutil) plus urandom hex. They NEVER encode account identifiers, hostnames, usernames,
    API data, or portfolio fields.
  * Hash helpers read files; they never modify anything and never return file CONTENTS.
  * Timestamp parsing is strict (aware-UTC or 'YYYY-MM-DD HH:MM:SS UTC'); malformed input
    returns None so callers fail closed rather than guessing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tzutil  # noqa: E402

RUN_ID_RE = re.compile(r"^[a-z]+-\d{8}T\d{6}Z-[0-9a-f]{12}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def canonical_json(obj) -> str:
    """Deterministic serialization for hashing (sorted keys, tight separators, ASCII)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_file(path: Path) -> str | None:
    """Hex sha256 of a file's exact bytes; None when unreadable (caller fails closed)."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def sha256_tree(root: Path, pattern: str = "*.jsonl") -> dict:
    """Deterministic directory-snapshot digest, FAIL-CLOSED:
      {"ok": True,  "digest": <sha256>, "file_count": n}                      readable tree
      {"ok": True,  "digest": <sha256-of-empty-list>, "file_count": 0}        existing EMPTY dir
      {"ok": False, "reason": "archive_root_missing",   "digest": None}       root absent
      {"ok": False, "reason": "archive_tree_unreadable", "digest": None,
       "unreadable_count": n}                                                 any hash failure
    A digest is NEVER produced over entries containing null — an unreadable file must never
    silently vanish into a 'valid' fingerprint. File contents are never returned or logged."""
    root = Path(root)
    if not root.is_dir():
        return {"ok": False, "reason": "archive_root_missing", "digest": None}
    entries = []
    unreadable = 0
    for p in sorted(root.rglob(pattern)):
        if p.is_file():
            digest = sha256_file(p)
            if digest is None:
                unreadable += 1
                continue
            entries.append([str(p.relative_to(root)), digest])
    if unreadable:
        return {"ok": False, "reason": "archive_tree_unreadable", "digest": None,
                "unreadable_count": unreadable}
    return {"ok": True,
            "digest": hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest(),
            "file_count": len(entries)}


def parse_utc_strict(value) -> dt.datetime | None:
    """Aware-UTC datetime from ISO8601 (aware only) or 'YYYY-MM-DD HH:MM:SS UTC'; None on
    anything malformed, naive, or non-string — never a guess."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def plausible_generated_for(generated_utc, through: str, max_lag_days: int = 30) -> bool:
    """True when generated_utc parses AND its UTC date lies in [through, through+max_lag_days].
    Deterministic (no wall-clock dependence): rejects far-future forgeries ('9999-...') and
    generation stamps earlier than the session they claim to cover."""
    parsed = parse_utc_strict(generated_utc)
    if parsed is None or not isinstance(through, str) or not _DATE_RE.match(through):
        return False
    try:
        t = dt.date.fromisoformat(through)
    except ValueError:
        return False
    return t <= parsed.date() <= t + dt.timedelta(days=max_lag_days)


def run_id_timestamp(value: str) -> dt.datetime | None:
    """The run ID's embedded timestamp as a REAL aware-UTC datetime, or None. Parsing with
    strptime (not regex arithmetic) rejects zero dates ('00000000'), impossible dates
    ('20260231'), and impossible times — the exact class of the reproduced
    postclose-00000000T000000Z fallback bug."""
    if not isinstance(value, str):
        return None
    m = RUN_ID_RE.match(value)
    if not m:
        return None
    stamp = value.split("-")[1]
    try:
        parsed = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    if parsed.year < 2020:   # this system did not exist before 2020; reject degenerate stamps
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def valid_run_id(value, expected_prefix: str | None = None) -> bool:
    """Strict: format AND a real, plausible embedded UTC timestamp AND (optionally) an exact
    prefix. A syntactically shaped but temporally impossible ID is INVALID."""
    ts = run_id_timestamp(value)
    if ts is None:
        return False
    if expected_prefix is not None and not value.startswith(expected_prefix + "-"):
        return False
    return True


def valid_postclose_run_id(value, target: str, max_lag_days: int = 3) -> bool:
    """Target-aware: a post-close run ID must carry the 'postclose' prefix and its embedded
    UTC stamp must fall on the target's PT session date (or within max_lag_days after, for
    late manual reruns). PT conversion goes through tzutil/zoneinfo — never manual offsets."""
    ts = run_id_timestamp(value)
    if ts is None or not valid_run_id(value, expected_prefix="postclose"):
        return False
    if not isinstance(target, str) or not _DATE_RE.match(target):
        return False
    try:
        t = dt.date.fromisoformat(target)
    except ValueError:
        return False
    pt_date = tzutil.to_pt(ts).date()
    return t <= pt_date <= t + dt.timedelta(days=max_lag_days)


def new_run_id(prefix: str = "postclose") -> str:
    """Opaque, unique, filesystem-safe per-invocation ID: <prefix>-<UTCstamp>-<random hex>.
    Contains NO host/user/account/API information. Always passes its own strict validator."""
    if not re.match(r"^[a-z]+$", prefix):
        raise ValueError("run_id_prefix_must_be_lowercase_alpha")
    stamp = tzutil.now_utc().strftime("%Y%m%dT%H%M%SZ")
    rid = f"{prefix}-{stamp}-{os.urandom(6).hex()}"
    if not valid_run_id(rid, expected_prefix=prefix):
        raise ValueError("run_id_self_validation_failed")
    return rid


def safety_flags_ok(doc: dict, *, required_true=("diagnostic_only",),
                    required_false=("execution_enabled",)) -> bool:
    """Exact-boolean check of the non-negotiable safety flags."""
    if not isinstance(doc, dict):
        return False
    return (all(doc.get(k) is True for k in required_true)
            and all(doc.get(k) is False for k in required_false))
