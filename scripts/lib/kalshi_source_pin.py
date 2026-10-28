#!/usr/bin/env python3
"""Single authoritative pin for the external Kalshi MCP server source.

WHY
---
The expected digest `a03b249f…` was independently copied into THREE places:

  * scripts/broker_runtime_audit.py   (KALSHI_EXPECTED_SOURCE_SHA256 literal)
  * scripts/kalshi/lib/kec_connector.py (PINNED_SERVER_SHA256 literal)
  * scripts/kalshi/config/connector_policy.json (source_integrity.sha256)

and the absolute path into two. They agreed by luck, and nothing enforced agreement across the
boundary: `broker_runtime_audit` never read the JSON, and `kec_connector.validate_policy`
compared the JSON only to its OWN constant. A rotation that updated two of the three would leave
the runtime classifier pinned to a stale digest — failing closed, but silently killing every
Kalshi read with no diagnostic pointing at the mismatch.

This module makes `scripts/kalshi/config/connector_policy.json` the ONE authoritative source and
exposes it to every consumer. Rotating the pin is now a single edit to that JSON.

FAIL-CLOSED
-----------
Missing policy, unreadable policy, malformed JSON, missing field, a digest that is not exactly
64 lowercase hex characters, a non-absolute path, or `enforce_sha256` disabled all yield
`expected_sha256() is None`, and `source_current()` is then False. There is NO fallback to an
unpinned source and no default digest: absence never grants authority.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import secure_hash

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "scripts" / "kalshi" / "config" / "connector_policy.json"

# The external server is a single small Python module; 2 MiB is far above its real size and far
# below anything that could stall an audited lane.
MAX_SOURCE_BYTES = 2_000_000
# Neutral placeholder location (public snapshot); the private deployment pins its real checkout.
SOURCE_PATH = Path.home() / "kalshi-mcp" / "server.py"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _policy() -> dict | None:
    digest_guard = secure_hash.sha256_file_strict(POLICY_PATH, MAX_SOURCE_BYTES)
    if digest_guard is None:
        return None                      # missing / symlinked / oversized policy -> no authority
    try:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def expected_sha256() -> str | None:
    """The one authoritative expected digest, or None when policy cannot grant authority."""
    document = _policy()
    if document is None:
        return None
    integrity = document.get("source_integrity")
    if not isinstance(integrity, dict):
        return None
    if integrity.get("enforce_sha256") is not True:
        return None                      # enforcement off is treated as no pin at all
    value = integrity.get("sha256")
    if not isinstance(value, str) or not _SHA_RE.match(value):
        return None                      # malformed digest fails closed
    return value


def source_path() -> Path | None:
    """Absolute path of the pinned external source, or None when it cannot be trusted."""
    if not SOURCE_PATH.is_absolute():
        return None
    return SOURCE_PATH


def current_sha256() -> str | None:
    """Strictly hashed digest of the external source as it exists right now."""
    path = source_path()
    if path is None:
        return None
    return secure_hash.sha256_file_strict(path, MAX_SOURCE_BYTES)


def source_current() -> bool:
    """True only when the live source hashes to the single authoritative expected digest."""
    expected = expected_sha256()
    if expected is None:
        return False
    actual = current_sha256()
    return actual is not None and actual == expected
