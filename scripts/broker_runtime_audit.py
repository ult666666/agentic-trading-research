#!/usr/bin/env python3
"""Runtime broker-tool attempt audit for unattended Claude sessions.

An additional Claude settings file routes SessionStart and every MCP PreToolUse event here. Raw
tool inputs are never persisted: only the exact tool name, classification, and canonical input
hash are retained. Known preview/write tools and unknown broker-shaped tools are denied before
execution. Existing project permission denies remain authoritative and unchanged.

The per-invocation receipt distinguishes `complete` instrumentation from `unobservable`; absence
of a heartbeat can never be reported as zero attempts.

CLI (hook commands are normally invoked by Claude Code):
  broker_runtime_audit.py hook-session
  broker_runtime_audit.py hook-tool
  broker_runtime_audit.py validate-receipt --receipt PATH
  broker_runtime_audit.py summarize --target YYYY-MM-DD [--root PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import validate_broker_permissions as permission_policy  # noqa: E402

EVENT_SCHEMA = "broker_runtime_event/1.0"
RECEIPT_SCHEMA = "broker_runtime_audit_receipt/1.0"
import secure_hash as _secure_hash

SLI_SCHEMA = "broker_runtime_sli/1.0"
DEFAULT_AUDIT_ROOT = ROOT / "data" / "runtime" / "broker_audit"
SETTINGS_PATH = ROOT / "config" / "claude_broker_audit_settings.json"
SNAPSHOT_PATH = ROOT / "data" / "broker_tool_namespace.json"
KALSHI_CONNECTOR_POLICY_PATH = ROOT / "scripts" / "kalshi" / "config" / "connector_policy.json"
# The path and expected digest are NOT defined here. They were previously duplicated across three
# files with nothing enforcing agreement, so a partial rotation would have left this classifier
# pinned to a stale digest — failing closed but silently killing every Kalshi read with no
# diagnostic. scripts/lib/kalshi_source_pin.py reads the single authority
# (scripts/kalshi/config/connector_policy.json); missing or malformed policy yields None, and a
# None expectation can never match a digest, so absence still fails closed.
import kalshi_source_pin as _kalshi_pin

KALSHI_SERVER_SOURCE_PATH = _kalshi_pin.SOURCE_PATH
KALSHI_EXPECTED_SOURCE_SHA256 = _kalshi_pin.expected_sha256()
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_SOURCE_BYTES = 8 * 1024 * 1024
BROKER_SERVER_HINT = re.compile(
    r"(?:robinhood|broker|ibkr|interactive[_-]?brokers|kalshi|"
    r"event[_-]?market|prediction[_-]?market)",
    re.I,
)
KALSHI_PUBLIC_READ_TOOLS = {
    "mcp__kalshi__get_exchange_status",
    "mcp__kalshi__list_markets",
    "mcp__kalshi__get_market",
    "mcp__kalshi__get_orderbook",
    "mcp__kalshi__get_recent_trades",
    "mcp__kalshi__get_candlesticks",
    "mcp__kalshi__list_events",
    "mcp__kalshi__get_event",
    "mcp__kalshi__list_series",
    "mcp__kalshi__get_series",
}
KALSHI_PUBLIC_READ_BASENAMES = {
    permission_policy._basename(tool_name) for tool_name in KALSHI_PUBLIC_READ_TOOLS
}
KALSHI_PRIVATE_ACCOUNT_BASENAMES = {
    "get_balance",
    "get_positions",
    "get_orders",
    "get_fills",
    "get_settlements",
}
KALSHI_DISABLED_ACCOUNT_READ_TOOLS = {
    f"mcp__kalshi__{basename}" for basename in KALSHI_PRIVATE_ACCOUNT_BASENAMES
}
# Unrecognized MCP namespaces fail closed in unattended trading lanes. This deliberately tiny
# set records exact tools that are positively known to be non-financial; adding one is a policy
# change and therefore changes policy_sha256.
KNOWN_NON_BROKER_READ_TOOLS = frozenset({"mcp__github__search_repositories"})
NAMESPACE_REGISTRY_HEALTHY = "healthy"
EVENT_TYPES = {"wrapper_started", "session_start", "mcp_tool_attempt"}
CLASSIFICATIONS = {
    "not_applicable", "broker_read_only", "broker_preview_or_write",
    "unknown_broker_tool", "non_broker_mcp",
}
SAFETY = {
    "research_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "raw_tool_inputs_persisted": False,
    "contains_account_data": False,
    "contains_credentials": False,
    "broker_preview_write_allowed": False,
}
SLI_KEYS = {
    "schema", "target_session_date", "generated_utc", "status", "invariant",
    "receipt_count", "complete_receipt_count", "incomplete_receipt_count",
    "invalid_receipt_count", "broker_read_attempt_total",
    "broker_preview_write_attempt_total", "unknown_broker_attempt_total",
    "non_broker_mcp_attempt_total", "zero_preview_write_verified",
    "source_receipts", "invalid_receipts", "safety", "sli_sha256",
}
SLI_SOURCE_KEYS = {"path", "receipt_sha256", "coverage"}
SLI_INVALID_SOURCE_KEYS = {"path_sha256", "errors"}
ENV_KEYS = {
    "root": "TRADING_BROKER_AUDIT_ROOT",
    "target": "TRADING_BROKER_AUDIT_TARGET",
    "workflow": "TRADING_BROKER_AUDIT_WORKFLOW",
    "run_id": "TRADING_BROKER_AUDIT_RUN_ID",
    "invocation_id": "TRADING_BROKER_AUDIT_INVOCATION_ID",
    "event_log": "TRADING_BROKER_AUDIT_EVENT_LOG",
}


class AuditError(RuntimeError):
    """Fail-closed runtime-audit refusal."""


class AuditViolation(AuditError):
    """A prior or current broker runtime violation; no further model lane may start."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str | None:
    """Fail-closed digest: rejects symlinks, non-regular files, empty and oversized files.

    Delegates to scripts/lib/secure_hash.py. The previous implementation was read_bytes(), which
    followed symlinks and had no size bound — a hook hashing a FIFO could hang the whole audited
    lane. Returning None on every rejection keeps callers' pin comparisons fail-closed.
    """
    return _secure_hash.sha256_file_strict(path, MAX_SOURCE_BYTES)


def _sha_file_legacy(path: Path) -> str | None:
    try:
        return _sha_bytes(path.read_bytes())
    except OSError:
        return None


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate_json_key:{key}")
        document[key] = value
    return document


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _strict_path(path: Path, root: Path, *, kind: str) -> bool:
    """Check lexical containment and reject every symlink from ``root`` to ``path``."""
    base = _lexical_absolute(root)
    candidate = _lexical_absolute(path)
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return False
    current = base
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    if kind == "file":
        return candidate.is_file()
    if kind == "directory":
        return candidate.is_dir()
    if kind == "missing_or_directory":
        return not candidate.exists() or candidate.is_dir()
    raise ValueError("strict_path_kind_invalid")


def _read_json_file_strict(path: Path, root: Path) -> tuple[dict | None, list[str]]:
    if not _strict_path(path, root, kind="file"):
        return None, ["source_path_not_canonical"]
    try:
        size = path.stat().st_size
        if size < 1 or size > MAX_SOURCE_BYTES:
            return None, ["source_size_invalid"]
        raw = path.read_bytes()
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, ["source_json_invalid"]
    if not isinstance(document, dict):
        return None, ["source_not_object"]
    return document, []


def _without(document: dict, key: str) -> dict:
    return {name: value for name, value in document.items() if name != key}


def _utc(now: dt.datetime | None = None) -> str:
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise AuditError("timestamp_timezone_missing")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise AuditError("target_invalid")
    try:
        if dt.date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise AuditError("target_invalid") from exc
    return value


def _token(value: Any, field: str) -> str:
    text = str(value or "")
    if not TOKEN_RE.fullmatch(text):
        raise AuditError(f"{field}_invalid")
    return text


def _parse_namespace_registry(document: Any) -> tuple[frozenset[str], str]:
    """Validate the broker namespace snapshot used by runtime classification.

    An empty set is never a valid registry.  Returning an explicit state lets callers preserve
    the reason for fail-closed behavior in the policy hash without throwing during hook import.
    """
    if not isinstance(document, dict) or not document:
        return frozenset(), "namespace_root_invalid"
    if document.get("schema_version") != "1.0":
        return frozenset(), "namespace_schema_invalid"

    recognized = document.get("recognized_broker_servers")
    if not isinstance(recognized, list) or not recognized:
        return frozenset(), "namespace_recognized_servers_invalid"
    if any(
            not isinstance(server, str)
            or not server
            or "__" in server
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", server)
            for server in recognized):
        return frozenset(), "namespace_recognized_servers_invalid"
    recognized_servers = frozenset(recognized)
    if len(recognized_servers) != len(recognized):
        return frozenset(), "namespace_recognized_servers_invalid"

    servers = document.get("servers")
    if not isinstance(servers, dict) or not servers:
        return frozenset(), "namespace_servers_invalid"
    seen_tools: set[str] = set()
    for server, server_document in servers.items():
        if server not in recognized_servers or not isinstance(server_document, dict):
            return frozenset(), "namespace_server_record_invalid"
        tools = server_document.get("tools")
        if not isinstance(tools, dict) or not tools:
            return frozenset(), "namespace_server_tools_invalid"
        for category, names in tools.items():
            if not isinstance(category, str) or not category:
                return frozenset(), "namespace_tool_category_invalid"
            if not isinstance(names, list) or not names:
                return frozenset(), "namespace_tool_list_invalid"
            for tool_name in names:
                if (not isinstance(tool_name, str)
                        or not TOOL_RE.fullmatch(tool_name)
                        or permission_policy._server_of(tool_name) != server
                        or tool_name in seen_tools):
                    return frozenset(), "namespace_tool_record_invalid"
                seen_tools.add(tool_name)
    if not seen_tools:
        return frozenset(), "namespace_tools_empty"
    # recognized_broker_servers may intentionally contain a newly observed alias without a
    # tool inventory. That alias is still safe: classify_tool() sends every method under any
    # recognized server that is not on the exact read allowlist to unknown_broker_tool.
    return recognized_servers, NAMESPACE_REGISTRY_HEALTHY


def _load_namespace_registry(
        path: Path = SNAPSHOT_PATH, *, root: Path = ROOT,
) -> tuple[frozenset[str], str]:
    document, errors = _read_json_file_strict(path, root)
    if errors:
        return frozenset(), f"namespace_{errors[0]}"
    return _parse_namespace_registry(document)


RECOGNIZED_SERVERS, NAMESPACE_REGISTRY_STATE = _load_namespace_registry()
IMPORTED_SETTINGS_SHA256 = _sha_file(SETTINGS_PATH)
IMPORTED_NAMESPACE_SHA256 = _sha_file(SNAPSHOT_PATH)
IMPORTED_KALSHI_CONNECTOR_POLICY_SHA256 = _sha_file(KALSHI_CONNECTOR_POLICY_PATH)


def _control_plane_files_current() -> bool:
    """Reject a post-import control-file swap before any stale classification can run."""
    return (
        IMPORTED_SETTINGS_SHA256 is not None
        and IMPORTED_NAMESPACE_SHA256 is not None
        and IMPORTED_KALSHI_CONNECTOR_POLICY_SHA256 is not None
        and _sha_file(SETTINGS_PATH) == IMPORTED_SETTINGS_SHA256
        and _sha_file(SNAPSHOT_PATH) == IMPORTED_NAMESPACE_SHA256
        and _sha_file(KALSHI_CONNECTOR_POLICY_PATH)
        == IMPORTED_KALSHI_CONNECTOR_POLICY_SHA256
    )


def _kalshi_source_current() -> bool:
    return _sha_file(KALSHI_SERVER_SOURCE_PATH) == KALSHI_EXPECTED_SOURCE_SHA256


def classify_tool(tool_name: str) -> str:
    if not TOOL_RE.fullmatch(tool_name):
        raise AuditError("tool_name_invalid")
    if not _control_plane_files_current():
        return "unknown_broker_tool"
    if NAMESPACE_REGISTRY_STATE != NAMESPACE_REGISTRY_HEALTHY:
        return "unknown_broker_tool"
    if tool_name in KALSHI_PUBLIC_READ_TOOLS:
        return "broker_read_only" if _kalshi_source_current() else "unknown_broker_tool"
    server = permission_policy._server_of(tool_name)
    basename = permission_policy._basename(tool_name)
    # A connector rename must never turn a known Kalshi-shaped method into an unrelated MCP
    # read. Only the exact, reviewed namespace above is readable; the same basename on every
    # other namespace is an unknown broker tool until an operator binds that namespace.
    if basename in KALSHI_PUBLIC_READ_BASENAMES:
        return "unknown_broker_tool"
    if basename in KALSHI_PRIVATE_ACCOUNT_BASENAMES:
        return "unknown_broker_tool"
    if tool_name in permission_policy.READ_ALLOWLIST:
        return "broker_read_only"
    if tool_name in permission_policy.KNOWN_MUTATION:
        return "broker_preview_or_write"
    if server == "kalshi":
        return "unknown_broker_tool"
    if server in RECOGNIZED_SERVERS:
        return "unknown_broker_tool"
    if BROKER_SERVER_HINT.search(server):
        return "unknown_broker_tool"
    if basename not in permission_policy.WRITE_LIKE_FALSE_POSITIVES \
            and permission_policy.WRITE_LIKE_RE.search(basename):
        return "unknown_broker_tool"
    if tool_name in KNOWN_NON_BROKER_READ_TOOLS:
        return "non_broker_mcp"
    return "unknown_broker_tool"


def policy_sha256() -> str:
    values = {
        "settings_sha256": _sha_file(SETTINGS_PATH),
        "namespace_sha256": _sha_file(SNAPSHOT_PATH),
        "kalshi_connector_policy_sha256": _sha_file(KALSHI_CONNECTOR_POLICY_PATH),
        "kalshi_server_source_sha256": _sha_file(KALSHI_SERVER_SOURCE_PATH),
        "kalshi_expected_source_sha256": KALSHI_EXPECTED_SOURCE_SHA256,
        "kalshi_source_current": _kalshi_source_current(),
        "imported_settings_sha256": IMPORTED_SETTINGS_SHA256,
        "imported_namespace_sha256": IMPORTED_NAMESPACE_SHA256,
        "imported_kalshi_connector_policy_sha256":
            IMPORTED_KALSHI_CONNECTOR_POLICY_SHA256,
        "control_plane_files_current": _control_plane_files_current(),
        "namespace_registry_state": NAMESPACE_REGISTRY_STATE,
        "recognized_broker_servers": sorted(RECOGNIZED_SERVERS),
        "read_allowlist": sorted(permission_policy.READ_ALLOWLIST),
        "known_mutation": sorted(permission_policy.KNOWN_MUTATION),
        "write_like_patterns": list(permission_policy.WRITE_LIKE_PATTERNS),
        "kalshi_public_read_tools": sorted(KALSHI_PUBLIC_READ_TOOLS),
        "kalshi_private_account_basenames": sorted(KALSHI_PRIVATE_ACCOUNT_BASENAMES),
        "kalshi_disabled_account_read_tools": sorted(KALSHI_DISABLED_ACCOUNT_READ_TOOLS),
        "known_non_broker_read_tools": sorted(KNOWN_NON_BROKER_READ_TOOLS),
    }
    return _sha(values)


def invocation_paths(audit_root: Path, target: str, run_id: str,
                     invocation_id: str) -> dict[str, Path]:
    target = _date(target)
    run_id = _token(run_id, "run_id")
    invocation_id = _token(invocation_id, "invocation_id")
    root = audit_root.resolve()
    run_root = root / target / run_id
    paths = {
        "run_root": run_root,
        "event_log": run_root / f"{invocation_id}.events.jsonl",
        "receipt": run_root / f"{invocation_id}.receipt.json",
        "lock": run_root / f".{invocation_id}.lock",
        "sli": root / target / "sli.json",
        "sli_lock": root / target / ".sli.lock",
    }
    for path in paths.values():
        path.resolve().relative_to(root)
    return paths


def _context_from_env() -> dict:
    missing = [name for name, env_name in ENV_KEYS.items() if not os.environ.get(env_name)]
    if missing:
        raise AuditError("audit_environment_missing:" + ",".join(missing))
    root = Path(os.environ[ENV_KEYS["root"]]).resolve()
    target = _date(os.environ[ENV_KEYS["target"]])
    workflow = _token(os.environ[ENV_KEYS["workflow"]], "workflow")
    run_id = _token(os.environ[ENV_KEYS["run_id"]], "run_id")
    invocation_id = _token(os.environ[ENV_KEYS["invocation_id"]], "invocation_id")
    paths = invocation_paths(root, target, run_id, invocation_id)
    supplied = Path(os.environ[ENV_KEYS["event_log"]]).resolve()
    if supplied != paths["event_log"].resolve():
        raise AuditError("audit_event_log_path_mismatch")
    return {"audit_root": root, "target": target, "workflow": workflow,
            "run_id": run_id, "invocation_id": invocation_id, **paths}


def _event(context: dict, event_type: str, *, session_id: Any = None,
           tool_name: str | None = None, tool_input: Any = None,
           classification: str = "not_applicable", now: dt.datetime | None = None) -> dict:
    if event_type not in EVENT_TYPES or classification not in CLASSIFICATIONS:
        raise AuditError("event_type_or_classification_invalid")
    session_hash = _sha(str(session_id)) if isinstance(session_id, str) and session_id else None
    input_hash = _sha(tool_input) if event_type == "mcp_tool_attempt" else None
    return {
        "schema": EVENT_SCHEMA,
        "seq": 0,
        "event_type": event_type,
        "target_session_date": context["target"],
        "workflow": context["workflow"],
        "run_id": context["run_id"],
        "invocation_id": context["invocation_id"],
        "generated_utc": _utc(now),
        "session_id_sha256": session_hash,
        "tool_name": tool_name,
        "tool_input_sha256": input_hash,
        "classification": classification,
        "previous_event_sha256": None,
        "policy_sha256": policy_sha256(),
        "safety": dict(SAFETY),
    }


def validate_event(document: Any) -> list[str]:
    expected = {
        "schema", "seq", "event_type", "target_session_date", "workflow", "run_id",
        "invocation_id", "generated_utc", "session_id_sha256", "tool_name",
        "tool_input_sha256", "classification", "previous_event_sha256", "policy_sha256",
        "safety", "event_sha256",
    }
    if not isinstance(document, dict):
        return ["event_not_object"]
    errors: list[str] = []
    if set(document) != expected:
        errors.append("event_fields_invalid")
    if document.get("schema") != EVENT_SCHEMA:
        errors.append("event_schema_invalid")
    if not isinstance(document.get("seq"), int) or document.get("seq", -1) < 0:
        errors.append("event_seq_invalid")
    if document.get("event_type") not in EVENT_TYPES:
        errors.append("event_type_invalid")
    if document.get("classification") not in CLASSIFICATIONS:
        errors.append("event_classification_invalid")
    if document.get("safety") != SAFETY:
        errors.append("event_safety_invalid")
    try:
        _date(document.get("target_session_date"))
        _token(document.get("workflow"), "workflow")
        _token(document.get("run_id"), "run_id")
        _token(document.get("invocation_id"), "invocation_id")
    except AuditError as exc:
        errors.append(str(exc))
    if _parse_utc(document.get("generated_utc")) is None:
        errors.append("event_timestamp_invalid")
    for key in ("session_id_sha256", "tool_input_sha256", "previous_event_sha256"):
        if document.get(key) is not None and not SHA_RE.fullmatch(str(document.get(key))):
            errors.append(f"{key}_invalid")
    if not SHA_RE.fullmatch(str(document.get("policy_sha256") or "")):
        errors.append("policy_sha256_invalid")
    elif document.get("policy_sha256") != policy_sha256():
        errors.append("policy_sha256_stale")
    if document.get("event_type") == "mcp_tool_attempt":
        if not TOOL_RE.fullmatch(str(document.get("tool_name") or "")):
            errors.append("event_tool_name_invalid")
        if not SHA_RE.fullmatch(str(document.get("tool_input_sha256") or "")):
            errors.append("event_tool_input_hash_missing")
    elif document.get("tool_name") is not None or document.get("tool_input_sha256") is not None:
        errors.append("non_tool_event_has_tool_data")
    if document.get("event_sha256") != _sha(_without(document, "event_sha256")):
        errors.append("event_sha256_mismatch")
    return sorted(set(errors))


def read_events(path: Path) -> tuple[list[dict], list[str]]:
    if not path.is_file():
        return [], ["event_log_missing"]
    try:
        raw = path.read_bytes()
    except OSError:
        return [], ["event_log_unreadable"]
    if raw and not raw.endswith(b"\n"):
        return [], ["event_log_torn_tail"]
    events: list[dict] = []
    errors: list[str] = []
    previous = None
    for index, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line, object_pairs_hook=_duplicate_rejecting_object)
        except Exception:
            errors.append(f"line[{index}].json_invalid")
            continue
        if not isinstance(event, dict):
            errors.append(f"line[{index}].event_not_object")
            continue
        errors.extend(f"line[{index}].{value}" for value in validate_event(event))
        if event.get("seq") != index - 1:
            errors.append(f"line[{index}].sequence_invalid")
        if event.get("previous_event_sha256") != previous:
            errors.append(f"line[{index}].chain_invalid")
        previous = event.get("event_sha256")
        events.append(event)
    return events, sorted(set(errors))


def _append_event(context: dict, document: dict) -> dict:
    path = context["event_log"]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with context["lock"].open("a+b") as lock:
        os.chmod(context["lock"], 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            events, errors = read_events(path)
            if errors:
                raise AuditError("event_log_invalid_before_append:" + ",".join(errors[:5]))
        else:
            events = []
        document["seq"] = len(events)
        document["previous_event_sha256"] = events[-1]["event_sha256"] if events else None
        document["event_sha256"] = _sha(document)
        errors = validate_event(document)
        if errors:
            raise AuditError("event_invalid:" + ",".join(errors[:5]))
        encoded = _canonical(document) + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, encoded) != len(encoded):
                raise AuditError("event_append_short")
            os.fsync(fd)
        finally:
            os.close(fd)
    return document


def start_invocation(*, audit_root: Path, target: str, workflow: str,
                     run_id: str, invocation_id: str,
                     now: dt.datetime | None = None) -> dict:
    paths = invocation_paths(audit_root, target, run_id, invocation_id)
    context = {"audit_root": audit_root.resolve(), "target": _date(target),
               "workflow": _token(workflow, "workflow"),
               "run_id": _token(run_id, "run_id"),
               "invocation_id": _token(invocation_id, "invocation_id"), **paths}
    if paths["event_log"].exists() or paths["receipt"].exists():
        raise AuditError("invocation_artifact_collision")
    _append_event(context, _event(context, "wrapper_started", now=now))
    return context


def hook_session(payload: dict, *, now: dt.datetime | None = None) -> dict:
    context = _context_from_env()
    if payload.get("hook_event_name") != "SessionStart":
        raise AuditError("session_hook_event_invalid")
    return _append_event(context, _event(
        context, "session_start", session_id=payload.get("session_id"), now=now))


def hook_tool(payload: dict, *, now: dt.datetime | None = None) -> tuple[dict, bool]:
    context = _context_from_env()
    if payload.get("hook_event_name") != "PreToolUse":
        raise AuditError("tool_hook_event_invalid")
    tool_name = payload.get("tool_name")
    classification = classify_tool(tool_name)
    event = _append_event(context, _event(
        context, "mcp_tool_attempt", session_id=payload.get("session_id"),
        tool_name=tool_name, tool_input=payload.get("tool_input"),
        classification=classification, now=now))
    denied = classification in {"broker_preview_or_write", "unknown_broker_tool"}
    return event, denied


def _atomic_write(path: Path, document: dict, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = json.dumps(document, indent=2, sort_keys=True).encode() + b"\n"
    if create_only:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def finalize_invocation(context: dict, *, child_exit_code: int,
                        started_utc: str, finished_utc: str) -> dict:
    events, event_errors = read_events(context["event_log"])
    counts = {name: 0 for name in CLASSIFICATIONS}
    for event in events:
        counts[event.get("classification")] = counts.get(event.get("classification"), 0) + 1
    heartbeats = sum(1 for event in events if event.get("event_type") == "session_start")
    coverage = "complete" if not event_errors and heartbeats >= 1 else "unobservable"
    preview_write = counts["broker_preview_or_write"]
    unknown = counts["unknown_broker_tool"]
    violation = preview_write > 0 or unknown > 0
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "target_session_date": context["target"],
        "workflow": context["workflow"],
        "run_id": context["run_id"],
        "invocation_id": context["invocation_id"],
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "child_exit_code": int(child_exit_code),
        "audit_settings_sha256": _sha_file(SETTINGS_PATH),
        "policy_sha256": policy_sha256(),
        "event_log_sha256": _sha_file(context["event_log"]),
        "event_count": len(events),
        "session_heartbeat_count": heartbeats,
        "broker_read_attempt_total": counts["broker_read_only"],
        "broker_preview_write_attempt_total": preview_write,
        "unknown_broker_attempt_total": unknown,
        "non_broker_mcp_attempt_total": counts["non_broker_mcp"],
        "instrumentation_coverage": coverage,
        "coverage_errors": event_errors if event_errors else (
            [] if heartbeats else ["session_heartbeat_missing"]),
        "zero_preview_write_verified": coverage == "complete" and not violation,
        "violation_detected": violation,
        "pretool_deny_enforced": True,
        "event_log_path": f"{context['run_id']}/{context['invocation_id']}.events.jsonl",
        "safety": dict(SAFETY),
    }
    receipt["receipt_sha256"] = _sha(receipt)
    errors = validate_receipt(receipt)
    if errors:
        raise AuditError("receipt_self_validation_failed:" + ",".join(errors[:5]))
    _atomic_write(context["receipt"], receipt, create_only=True)
    return receipt


def validate_receipt(document: Any) -> list[str]:
    expected = {
        "schema", "target_session_date", "workflow", "run_id", "invocation_id",
        "started_utc", "finished_utc", "child_exit_code", "audit_settings_sha256",
        "policy_sha256", "event_log_sha256", "event_count", "session_heartbeat_count",
        "broker_read_attempt_total", "broker_preview_write_attempt_total",
        "unknown_broker_attempt_total", "non_broker_mcp_attempt_total",
        "instrumentation_coverage", "coverage_errors", "zero_preview_write_verified",
        "violation_detected", "pretool_deny_enforced", "event_log_path", "safety",
        "receipt_sha256",
    }
    if not isinstance(document, dict):
        return ["receipt_not_object"]
    errors: list[str] = []
    if set(document) != expected:
        errors.append("receipt_fields_invalid")
    if document.get("schema") != RECEIPT_SCHEMA:
        errors.append("receipt_schema_invalid")
    if document.get("safety") != SAFETY:
        errors.append("receipt_safety_invalid")
    try:
        _date(document.get("target_session_date"))
        _token(document.get("workflow"), "workflow")
        _token(document.get("run_id"), "run_id")
        _token(document.get("invocation_id"), "invocation_id")
    except AuditError as exc:
        errors.append(str(exc))
    if _parse_utc(document.get("started_utc")) is None \
            or _parse_utc(document.get("finished_utc")) is None:
        errors.append("receipt_timestamp_invalid")
    for key in ("audit_settings_sha256", "policy_sha256", "event_log_sha256"):
        if not SHA_RE.fullmatch(str(document.get(key) or "")):
            errors.append(f"{key}_invalid")
    current_settings_sha256 = _sha_file(SETTINGS_PATH)
    if (
        current_settings_sha256 is None
        or document.get("audit_settings_sha256") != current_settings_sha256
    ):
        errors.append("audit_settings_sha256_stale")
    if document.get("policy_sha256") != policy_sha256():
        errors.append("policy_sha256_stale")
    for key in ("event_count", "session_heartbeat_count", "broker_read_attempt_total",
                "broker_preview_write_attempt_total", "unknown_broker_attempt_total",
                "non_broker_mcp_attempt_total"):
        if not isinstance(document.get(key), int) or document[key] < 0:
            errors.append(f"{key}_invalid")
    if document.get("instrumentation_coverage") not in {"complete", "unobservable"}:
        errors.append("instrumentation_coverage_invalid")
    if not isinstance(document.get("coverage_errors"), list) \
            or any(not isinstance(value, str) for value in document["coverage_errors"]):
        errors.append("coverage_errors_invalid")
    violation = (document.get("broker_preview_write_attempt_total", 0) > 0
                 or document.get("unknown_broker_attempt_total", 0) > 0)
    expected_zero = document.get("instrumentation_coverage") == "complete" and not violation
    if document.get("violation_detected") is not violation:
        errors.append("violation_flag_invalid")
    if document.get("zero_preview_write_verified") is not expected_zero:
        errors.append("zero_verified_flag_invalid")
    if document.get("pretool_deny_enforced") is not True:
        errors.append("pretool_deny_flag_invalid")
    path = document.get("event_log_path")
    if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
        errors.append("event_log_path_invalid")
    if document.get("receipt_sha256") != _sha(_without(document, "receipt_sha256")):
        errors.append("receipt_sha256_mismatch")
    return sorted(set(errors))


def _validate_receipt_file(path: Path, audit_root: Path,
                           target: str) -> tuple[dict | None, list[str]]:
    """Validate one canonical receipt and re-derive its event-log projection."""
    base = _lexical_absolute(audit_root)
    target_root = base / target
    receipt, errors = _read_json_file_strict(path, base)
    if receipt is None:
        return None, sorted(set(["receipt_unreadable", *errors]))
    errors.extend(validate_receipt(receipt))
    if receipt.get("target_session_date") != target:
        errors.append("receipt_target_mismatch")

    run_id = receipt.get("run_id")
    invocation_id = receipt.get("invocation_id")
    expected_receipt = (target_root / str(run_id)
                        / f"{invocation_id}.receipt.json")
    if _lexical_absolute(path) != _lexical_absolute(expected_receipt):
        errors.append("receipt_path_binding_mismatch")
    expected_relative_event = f"{run_id}/{invocation_id}.events.jsonl"
    if receipt.get("event_log_path") != expected_relative_event:
        errors.append("receipt_event_log_path_binding_mismatch")
    expected_event = target_root / expected_relative_event
    if not _strict_path(expected_event, base, kind="file"):
        errors.append("receipt_event_log_path_not_canonical")
        return None, sorted(set(errors))
    try:
        if expected_event.stat().st_size > MAX_SOURCE_BYTES:
            errors.append("receipt_event_log_size_invalid")
            return None, sorted(set(errors))
    except OSError:
        errors.append("receipt_event_log_unreadable")
        return None, sorted(set(errors))
    if _sha_file(expected_event) != receipt.get("event_log_sha256"):
        errors.append("receipt_event_log_hash_mismatch")

    try:
        events, event_errors = read_events(expected_event)
    except Exception:
        errors.append("receipt_event_log_validation_exception")
        return None, sorted(set(errors))
    for index, event in enumerate(events):
        for key in ("target_session_date", "workflow", "run_id", "invocation_id"):
            if event.get(key) != receipt.get(key):
                errors.append(f"receipt_event_identity_mismatch:{index}:{key}")
        if event.get("policy_sha256") != receipt.get("policy_sha256"):
            errors.append(f"receipt_event_policy_mismatch:{index}")
        if event.get("event_type") == "mcp_tool_attempt":
            try:
                expected_classification = classify_tool(str(event.get("tool_name") or ""))
            except AuditError:
                errors.append(f"receipt_event_tool_invalid:{index}")
            else:
                if event.get("classification") != expected_classification:
                    errors.append(f"receipt_event_classification_mismatch:{index}")
        elif event.get("classification") != "not_applicable":
            errors.append(f"receipt_event_classification_mismatch:{index}")

    counts = {name: 0 for name in CLASSIFICATIONS}
    for event in events:
        classification = event.get("classification")
        counts[classification] = counts.get(classification, 0) + 1
    heartbeats = sum(1 for event in events if event.get("event_type") == "session_start")
    coverage = "complete" if not event_errors and heartbeats >= 1 else "unobservable"
    preview_write = counts["broker_preview_or_write"]
    unknown = counts["unknown_broker_tool"]
    violation = preview_write > 0 or unknown > 0
    expected_projection = {
        "event_log_sha256": _sha_file(expected_event),
        "event_count": len(events),
        "session_heartbeat_count": heartbeats,
        "broker_read_attempt_total": counts["broker_read_only"],
        "broker_preview_write_attempt_total": preview_write,
        "unknown_broker_attempt_total": unknown,
        "non_broker_mcp_attempt_total": counts["non_broker_mcp"],
        "instrumentation_coverage": coverage,
        "coverage_errors": event_errors if event_errors else (
            [] if heartbeats else ["session_heartbeat_missing"]),
        "zero_preview_write_verified": coverage == "complete" and not violation,
        "violation_detected": violation,
    }
    for key, expected in expected_projection.items():
        if receipt.get(key) != expected:
            errors.append(f"receipt_event_projection_mismatch:{key}")
    return (receipt if not errors else None), sorted(set(errors))


def _invalid_source(path: Path, target_root: Path, errors: list[str]) -> dict:
    try:
        label = str(_lexical_absolute(path).relative_to(_lexical_absolute(target_root)))
    except ValueError:
        label = path.name
    return {"path_sha256": _sha(label), "errors": sorted(set(errors))}


def _source_sli_projection(*, audit_root: Path,
                           target: str) -> tuple[dict | None, list[str]]:
    """Rebuild deterministic SLI fields from the exact canonical source set."""
    target = _date(target)
    base = _lexical_absolute(audit_root)
    if base.exists() and (base.is_symlink() or not base.is_dir()):
        return None, ["sli_source_audit_root_not_canonical"]
    target_root = base / target
    if not _strict_path(target_root, base, kind="missing_or_directory"):
        return None, ["sli_source_target_path_not_canonical"]

    receipt_paths: list[Path] = []
    event_paths: list[Path] = []
    invalid: list[dict] = []
    if target_root.is_dir():
        try:
            entries = sorted(target_root.iterdir(), key=lambda value: value.name)
        except OSError:
            return None, ["sli_source_target_unreadable"]
        for entry in entries:
            if entry.name in {"sli.json", ".sli.lock"}:
                continue
            if entry.is_symlink():
                invalid.append(_invalid_source(
                    entry, target_root, ["source_run_directory_not_canonical"]))
                continue
            if not entry.is_dir():
                continue
            try:
                children = sorted(entry.iterdir(), key=lambda value: value.name)
            except OSError:
                invalid.append(_invalid_source(
                    entry, target_root, ["source_run_directory_unreadable"]))
                continue
            receipt_paths.extend(
                child for child in children if child.name.endswith(".receipt.json"))
            event_paths.extend(
                child for child in children if child.name.endswith(".events.jsonl"))

    valid: list[tuple[Path, dict]] = []
    for path in sorted(receipt_paths):
        receipt, errors = _validate_receipt_file(path, base, target)
        if receipt is None:
            invalid.append(_invalid_source(path, target_root, errors))
        else:
            valid.append((path, receipt))

    receipt_counterparts = {
        path.with_name(path.name.removesuffix(".receipt.json") + ".events.jsonl")
        for path in receipt_paths
    }
    for path in sorted(event_paths):
        if path not in receipt_counterparts:
            reasons = ["receipt_missing_for_event_log"]
            if not _strict_path(path, base, kind="file"):
                reasons.append("source_event_log_path_not_canonical")
            invalid.append(_invalid_source(path, target_root, reasons))

    invalid_by_path: dict[str, set[str]] = {}
    for row in invalid:
        invalid_by_path.setdefault(row["path_sha256"], set()).update(row["errors"])
    invalid = [
        {"path_sha256": path_hash, "errors": sorted(reason_codes)}
        for path_hash, reason_codes in sorted(invalid_by_path.items())
    ]

    totals = {
        "broker_read_attempt_total": sum(
            receipt["broker_read_attempt_total"] for _, receipt in valid),
        "broker_preview_write_attempt_total": sum(
            receipt["broker_preview_write_attempt_total"] for _, receipt in valid),
        "unknown_broker_attempt_total": sum(
            receipt["unknown_broker_attempt_total"] for _, receipt in valid),
        "non_broker_mcp_attempt_total": sum(
            receipt["non_broker_mcp_attempt_total"] for _, receipt in valid),
    }
    incomplete = sum(
        1 for _, receipt in valid
        if receipt["instrumentation_coverage"] != "complete")
    if invalid or totals["broker_preview_write_attempt_total"] > 0 \
            or totals["unknown_broker_attempt_total"] > 0:
        status = "violation"
    elif not valid:
        status = "not_observed"
    elif incomplete:
        status = "unobservable"
    else:
        status = "zero_verified"
    source_receipts = [{
        "path": str(path.relative_to(target_root)),
        "receipt_sha256": receipt["receipt_sha256"],
        "coverage": receipt["instrumentation_coverage"],
    } for path, receipt in valid]
    projection = {
        "schema": SLI_SCHEMA,
        "target_session_date": target,
        "status": status,
        "invariant": "broker_preview_write_attempt_total == 0",
        "receipt_count": len(valid),
        "complete_receipt_count": len(valid) - incomplete,
        "incomplete_receipt_count": incomplete,
        "invalid_receipt_count": len(invalid),
        **totals,
        "zero_preview_write_verified": status == "zero_verified",
        "source_receipts": source_receipts,
        "invalid_receipts": invalid,
        "safety": dict(SAFETY),
    }
    return projection, []


def summarize_target(*, audit_root: Path, target: str,
                     now: dt.datetime | None = None, out: Path | None = None) -> dict:
    target = _date(target)
    audit_root = _lexical_absolute(audit_root)
    projection, source_errors = _source_sli_projection(
        audit_root=audit_root, target=target)
    if projection is None or source_errors:
        raise AuditError("sli_source_validation_failed:" + ",".join(source_errors))
    document = {**projection, "generated_utc": _utc(now)}
    document["sli_sha256"] = _sha(document)
    errors = validate_sli(document)
    if errors:
        raise AuditError("sli_self_validation_failed:" + ",".join(errors[:5]))
    destination = out or audit_root / target / "sli.json"
    _atomic_write(destination, document)
    return document


def validate_sli(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["sli_not_object"]
    errors: list[str] = []
    if set(document) != SLI_KEYS:
        errors.append("sli_fields_invalid")
    if document.get("schema") != SLI_SCHEMA:
        errors.append("sli_schema_invalid")
    try:
        _date(document.get("target_session_date"))
    except (AuditError, TypeError):
        errors.append("sli_target_invalid")
    if _parse_utc(document.get("generated_utc")) is None:
        errors.append("sli_timestamp_invalid")
    if document.get("safety") != SAFETY:
        errors.append("sli_safety_invalid")
    if document.get("status") not in {"zero_verified", "unobservable", "not_observed", "violation"}:
        errors.append("sli_status_invalid")
    if document.get("invariant") != "broker_preview_write_attempt_total == 0":
        errors.append("sli_invariant_invalid")
    for key in ("receipt_count", "complete_receipt_count", "incomplete_receipt_count",
                "invalid_receipt_count", "broker_read_attempt_total",
                "broker_preview_write_attempt_total", "unknown_broker_attempt_total",
                "non_broker_mcp_attempt_total"):
        if not isinstance(document.get(key), int) or isinstance(document.get(key), bool) \
                or document[key] < 0:
            errors.append(f"{key}_invalid")
    if document.get("zero_preview_write_verified") is not (document.get("status") == "zero_verified"):
        errors.append("sli_zero_flag_invalid")
    source_receipts = document.get("source_receipts")
    invalid_receipts = document.get("invalid_receipts")
    if not isinstance(source_receipts, list) or not isinstance(invalid_receipts, list):
        errors.append("sli_sources_invalid")
    else:
        paths: list[str] = []
        for row in source_receipts:
            if not isinstance(row, dict) or set(row) != SLI_SOURCE_KEYS:
                errors.append("sli_source_receipt_shape_invalid")
                continue
            path = row.get("path")
            lexical = Path(path) if isinstance(path, str) else None
            if lexical is None or lexical.is_absolute() or "." in lexical.parts \
                    or ".." in lexical.parts or len(lexical.parts) != 2 \
                    or not lexical.name.endswith(".receipt.json"):
                errors.append("sli_source_receipt_path_invalid")
            else:
                paths.append(path)
            if not isinstance(row.get("receipt_sha256"), str) \
                    or not SHA_RE.fullmatch(row["receipt_sha256"]):
                errors.append("sli_source_receipt_hash_invalid")
            if row.get("coverage") not in {"complete", "unobservable"}:
                errors.append("sli_source_receipt_coverage_invalid")
        if paths != sorted(set(paths)):
            errors.append("sli_source_receipts_not_canonical")
        valid_invalid_rows: list[dict] = []
        for row in invalid_receipts:
            if not isinstance(row, dict) or set(row) != SLI_INVALID_SOURCE_KEYS:
                errors.append("sli_invalid_receipt_shape_invalid")
                continue
            valid_invalid_rows.append(row)
            if not isinstance(row.get("path_sha256"), str) \
                    or not SHA_RE.fullmatch(row["path_sha256"]):
                errors.append("sli_invalid_receipt_hash_invalid")
            reasons = row.get("errors")
            if not isinstance(reasons, list) or reasons != sorted(set(reasons)) \
                    or any(not isinstance(reason, str) or not reason for reason in reasons):
                errors.append("sli_invalid_receipt_errors_invalid")
        if len(valid_invalid_rows) == len(invalid_receipts):
            invalid_paths = [row["path_sha256"] for row in valid_invalid_rows]
            if all(isinstance(value, str) for value in invalid_paths) \
                    and invalid_paths != sorted(set(invalid_paths)):
                errors.append("sli_invalid_receipts_not_canonical")
        receipt_count = document.get("receipt_count")
        invalid_count = document.get("invalid_receipt_count")
        complete_count = document.get("complete_receipt_count")
        incomplete_count = document.get("incomplete_receipt_count")
        if isinstance(receipt_count, int) and not isinstance(receipt_count, bool):
            if receipt_count != len(source_receipts):
                errors.append("sli_receipt_count_mismatch")
            if isinstance(complete_count, int) and isinstance(incomplete_count, int) \
                    and not isinstance(complete_count, bool) \
                    and not isinstance(incomplete_count, bool) \
                    and complete_count + incomplete_count != receipt_count:
                errors.append("sli_coverage_count_mismatch")
        if isinstance(invalid_count, int) and not isinstance(invalid_count, bool) \
                and invalid_count != len(invalid_receipts):
            errors.append("sli_invalid_count_mismatch")
    counts_valid = all(
        isinstance(document.get(key), int) and not isinstance(document.get(key), bool)
        and document[key] >= 0 for key in (
            "receipt_count", "complete_receipt_count", "incomplete_receipt_count",
            "invalid_receipt_count", "broker_preview_write_attempt_total",
            "unknown_broker_attempt_total"))
    if counts_valid:
        expected_status = (
            "violation" if document["invalid_receipt_count"] > 0
            or document["broker_preview_write_attempt_total"] > 0
            or document["unknown_broker_attempt_total"] > 0
            else "not_observed" if document["receipt_count"] == 0
            else "unobservable" if document["incomplete_receipt_count"] > 0
            else "zero_verified")
        if document.get("status") != expected_status:
            errors.append("sli_status_semantics_invalid")
    if document.get("sli_sha256") != _sha(_without(document, "sli_sha256")):
        errors.append("sli_sha256_mismatch")
    return sorted(set(errors))


def validate_sli_against_sources(document: Any, *, audit_root: Path,
                                 target: str) -> list[str]:
    """Require the SLI to equal a strict re-derivation of canonical receipts and logs."""
    errors = list(validate_sli(document))
    try:
        target = _date(target)
    except AuditError as exc:
        return sorted(set([*errors, f"sli_source_{exc}"]))
    if not isinstance(document, dict):
        return sorted(set([*errors, "sli_source_document_not_object"]))
    if document.get("target_session_date") != target:
        errors.append("sli_source_target_mismatch")
    projection, source_errors = _source_sli_projection(
        audit_root=audit_root, target=target)
    errors.extend(source_errors)
    if projection is None:
        return sorted(set(errors or ["sli_source_projection_unavailable"]))
    for key, expected in projection.items():
        if document.get(key) != expected:
            errors.append(f"sli_source_projection_mismatch:{key}")
    return sorted(set(errors))


def _read_hook_payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except Exception as exc:
        raise AuditError("hook_input_invalid") from exc
    if not isinstance(value, dict):
        raise AuditError("hook_input_not_object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Runtime broker-tool attempt audit.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook-session")
    sub.add_parser("hook-tool")
    validate = sub.add_parser("validate-receipt")
    validate.add_argument("--receipt", required=True)
    summary = sub.add_parser("summarize")
    summary.add_argument("--target", required=True)
    summary.add_argument("--root", default=str(DEFAULT_AUDIT_ROOT))
    summary.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        if args.command == "hook-session":
            hook_session(_read_hook_payload())
            return 0
        if args.command == "hook-tool":
            _event_row, denied = hook_tool(_read_hook_payload())
            if denied:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Broker preview/write or unknown broker tool is prohibited by the "
                            "research-only runtime audit boundary."
                        ),
                    }
                }, separators=(",", ":")))
            return 0
        if args.command == "validate-receipt":
            document = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
            errors = validate_receipt(document)
            result = {"ok": not errors, "errors": errors, **SAFETY}
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if not errors else 1
        result = summarize_target(
            audit_root=Path(args.root), target=args.target,
            out=Path(args.out) if args.out else None)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] != "violation" else 1
    except (AuditError, OSError, json.JSONDecodeError) as exc:
        # Exit 2 from PreToolUse blocks the call. Other events/commands fail visibly.
        print(f"broker_runtime_audit_error:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
