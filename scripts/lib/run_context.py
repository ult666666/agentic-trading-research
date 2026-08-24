#!/usr/bin/env python3
"""WI-2 runner-owned, child-write-denied run-context binding (2026-07-23).

WHY (receipt-bridge NOTES_NEXT WI-2 + adversarial finding 1, 2026-07-23 review): the
catalyst receipt validators locate their trusted receipts from BARE ENV
(`TRADING_RESEARCH_DIR`, `TRADING_TARGET_SESSION`/`TARGET_PT`).  The audited morning
model child INHERITS that env (audited_claude.py builds the child env from os.environ)
and its Seatbelt profile is deny-only for writes — the per-target receipt subdirectories
under `data/runtime/research/<target>/` remain CHILD-WRITABLE and every change there is
explicitly allowed by `_morning_authority_change_allowed`.  A post-activation child could
therefore forge receipts + collection seals with the public sealing code, or point env at
a directory of forgeries, and the outer runner's authoritative revalidation would bless
them.

FIX: the runner writes ONE small run-context file BEFORE the model child launches —
target session, runner-minted run nonce, audited run id, and a sha256 inventory of both
receipt-collection directories — into
`data/runtime/research/.publisher_locks/run_context_<target>.json`.

WHY THAT LOCATION (justified from the actual audited_claude.py + control_plane_integrity
code, not from convention):
  * `data/runtime/research/.publisher_locks` is a member of
    `control_plane_integrity.CONTROL_TREES`.  audited_claude.py builds the child's macOS
    Seatbelt deny profile from CONTROL_TREES (`_control_write_guard_profile`:
    `(deny file-write* (subpath ...))`), inherited by every descendant — the child cannot
    create, modify, or unlink anything under it.
  * The same tree is byte-captured by `capture_control_plane` before the child starts and
    compared + RESTORED after it exits; any bypass is a control-plane integrity violation
    (rc=88) and the pre-child bytes win.  The runner writes this file well before that
    capture, so it is part of the protected pre-child state.
  * The hook layer is explicitly NOT the boundary ("it parses command text and cannot be
    the security boundary" — audited_claude.py docstring), and the child profile does NOT
    block dot-directories in general: `data/runtime/research/<target>/.runner/` would be
    child-writable (research-prefix changes are allowed), so that alternative was
    rejected after reading the code.
  * The publisher-lock code (`research_freeze._publisher_lock`) opens only
    `<target>.lock` by exact name and never enumerates the directory, so an extra
    `run_context_<target>.json` cannot disturb locking.

Trust therefore derives from the WRITE-PROTECTED NAMESPACE, not from env: validators
refuse any run-context path outside `.../.publisher_locks/` (realpath-resolved), so a
forged env pointer to a child-writable copy fails closed.

ENFORCEMENT PHASING (both open user questions are built to the recommended default and
labeled PENDING USER RATIFICATION in the pack README):
  * enforcement="shadow"  (written by the runner until Pack-2): validators feature-detect
    the file but change NOTHING — behavior stays byte-identical to pre-WI-2.
  * enforcement="enforced" (Pack-2 flips the runner flag) or env
    TRADING_RUN_CONTEXT_REQUIRED=1: the new fail-closed denies fire
    (run_context_missing_or_invalid / run_context_binding_failed /
    manifest_inventory_check_failed).

This module is pure and offline: no network, no model, no broker surface; it grants
nothing by itself.  `execution_enabled` remains false everywhere.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "runner_run_context/1.0"
VERSION = "1.0.0"
PRODUCER = "scripts/lib/run_context.py"
# The child-write-denied control tree (control_plane_integrity.CONTROL_TREES member).
PROTECTED_SUBDIR_PARTS = ("data", "runtime", "research", ".publisher_locks")
ENV_PATH = "TRADING_RUN_CONTEXT_PATH"
ENV_REQUIRED = "TRADING_RUN_CONTEXT_REQUIRED"
ENV_TARGETS = ("TRADING_TARGET_SESSION", "TARGET_PT")
ENV_RESEARCH_DIR = "TRADING_RESEARCH_DIR"
ENFORCEMENT_SHADOW = "shadow"
ENFORCEMENT_ENFORCED = "enforced"
ENFORCEMENT_VALUES = (ENFORCEMENT_SHADOW, ENFORCEMENT_ENFORCED)
LANES = {
    "sec": {"subdir": "sec_catalyst_receipts", "collection": "_collection.json"},
    "news": {"subdir": "news_corroboration_receipts",
             "collection": "_news_collection.json"},
}
MAX_CONTEXT_BYTES = 2_000_000
MAX_INVENTORY_FILE_BYTES = 8_388_608
MAX_INVENTORY_ENTRIES = 4096
_TARGET_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SAFETY = {
    "feeds_production": False,
    "grants_source_authority": False,
    "grants_catalyst_confirmation": False,
    "execution_enabled": False,
    "network_invoked": False,
    "broker_invoked": False,
}
_LOADED_FROM_KEY = "_loaded_from"


# ---------------------------------------------------------------- canonical sealing
def canonical_sha256(document: dict) -> str:
    """SHA-256 over the context with its own artifact_sha256 removed, canonicalised
    exactly like the receipt lanes (sort_keys, compact separators, UTF-8)."""
    payload = {k: v for k, v in document.items()
               if k not in ("artifact_sha256", _LOADED_FROM_KEY)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_context(document: dict) -> dict:
    out = {k: v for k, v in document.items()
           if k not in ("artifact_sha256", _LOADED_FROM_KEY)}
    out["artifact_sha256"] = canonical_sha256(out)
    return out


def mint_nonce() -> str:
    return secrets.token_hex(16)


def context_file_name(target: str) -> str:
    return f"run_context_{target}.json"


def context_path(root: Path, target: str) -> Path:
    return Path(root).joinpath(*PROTECTED_SUBDIR_PARTS) / context_file_name(target)


def _sha_file(path: Path) -> str | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_INVENTORY_FILE_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _inventory_directory(directory: Path) -> dict:
    """Per-file sha256 snapshot of one receipt-collection directory (regular files only;
    symlinks/specials are recorded as irregular and can never satisfy a binding)."""
    result: dict[str, Any] = {"directory_present": False, "files": {},
                              "irregular_entries": []}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return result
    result["directory_present"] = True
    for name in entries:
        if len(result["files"]) >= MAX_INVENTORY_ENTRIES:
            result["irregular_entries"].append("inventory_truncated")
            break
        digest = _sha_file(directory / name)
        if digest is None:
            result["irregular_entries"].append(name)
        else:
            result["files"][name] = digest
    return result


def build_context(root: Path, *, target: str, audited_run_id: str, run_nonce: str,
                  enforcement: str = ENFORCEMENT_SHADOW,
                  generated_utc: str | None = None) -> dict:
    root = Path(root)
    if not _TARGET_RE.fullmatch(str(target)):
        raise ValueError("target_invalid")
    if not isinstance(audited_run_id, str) or not audited_run_id \
            or any(ord(c) < 32 for c in audited_run_id) \
            or len(audited_run_id.encode("utf-8")) > 512:
        raise ValueError("audited_run_id_invalid")
    if not isinstance(run_nonce, str) or not _NONCE_RE.fullmatch(run_nonce):
        raise ValueError("run_nonce_invalid")
    if enforcement not in ENFORCEMENT_VALUES:
        raise ValueError("enforcement_invalid")
    if generated_utc is None:
        generated_utc = dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    research = root / "data" / "runtime" / "research" / target
    collections = {
        lane: _inventory_directory(research / spec["subdir"])
        for lane, spec in LANES.items()
    }
    return seal_context({
        "schema": SCHEMA,
        "version": VERSION,
        "target_session_date": target,
        "run_nonce": run_nonce,
        "audited_run_id": audited_run_id,
        "enforcement": enforcement,
        "generated_utc": generated_utc,
        "research_dir": f"data/runtime/research/{target}",
        "collections": collections,
        "producer": PRODUCER,
        "safety": dict(SAFETY),
    })


def write_context(root: Path, *, target: str, audited_run_id: str, run_nonce: str,
                  enforcement: str = ENFORCEMENT_SHADOW,
                  generated_utc: str | None = None) -> Path:
    """Atomically write the runner-owned run-context into the protected namespace.

    Overwrite is allowed by design: only the outer runner (never the sandboxed child)
    can reach this tree, and a same-day catch-up run must be able to refresh the
    context before its own child launches."""
    document = build_context(
        root, target=target, audited_run_id=audited_run_id, run_nonce=run_nonce,
        enforcement=enforcement, generated_utc=generated_utc)
    destination = context_path(root, target)
    parent = destination.parent
    # Mirror research_freeze._publisher_lock: the namespace directory itself is 0700.
    grand = parent.parent
    grand.mkdir(parents=True, exist_ok=True)
    try:
        parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("run_context_directory_invalid")
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return destination


# ---------------------------------------------------------------- validator-side load
def load_context(environ: Any = None) -> tuple[dict | None, str | None]:
    """Feature-detected load: (document, None) on success; (None, None) when no
    run-context is exported (legacy pre-WI-2 behavior applies); (None, reason) when a
    context was exported but cannot be trusted.

    The path must realpath-resolve INSIDE a `data/runtime/research/.publisher_locks`
    directory — the child-write-denied control tree — so a forged env value pointing at
    a child-writable copy can never bind."""
    env = os.environ if environ is None else environ
    raw_path = env.get(ENV_PATH)
    if not raw_path:
        return None, None
    try:
        resolved = Path(os.path.realpath(raw_path))
    except (OSError, ValueError):
        return None, "path_unresolvable"
    parts = resolved.parent.parts
    if len(parts) < len(PROTECTED_SUBDIR_PARTS) \
            or tuple(parts[-len(PROTECTED_SUBDIR_PARTS):]) != PROTECTED_SUBDIR_PARTS:
        return None, "path_outside_protected_namespace"
    try:
        info = resolved.lstat()
    except OSError:
        return None, "file_missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "file_not_regular"
    if not 0 < info.st_size <= MAX_CONTEXT_BYTES:
        return None, "file_size_invalid"
    try:
        raw = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "file_unreadable"
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, "json_invalid"
    if not isinstance(document, dict):
        return None, "json_not_object"
    if document.get("schema") != SCHEMA or document.get("version") != VERSION:
        return None, "schema_invalid"
    if document.get("artifact_sha256") != canonical_sha256(document):
        return None, "self_hash_mismatch"
    target = document.get("target_session_date")
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        return None, "target_invalid"
    if resolved.name != context_file_name(target):
        return None, "filename_target_mismatch"
    if not isinstance(document.get("run_nonce"), str) \
            or not _NONCE_RE.fullmatch(document["run_nonce"]):
        return None, "run_nonce_invalid"
    run_id = document.get("audited_run_id")
    if not isinstance(run_id, str) or not run_id \
            or any(ord(c) < 32 for c in run_id) \
            or len(run_id.encode("utf-8")) > 512:
        return None, "audited_run_id_invalid"
    if document.get("enforcement") not in ENFORCEMENT_VALUES:
        return None, "enforcement_invalid"
    if document.get("research_dir") != f"data/runtime/research/{target}":
        return None, "research_dir_invalid"
    collections = document.get("collections")
    if not isinstance(collections, dict) or set(collections) != set(LANES):
        return None, "collections_invalid"
    for lane in LANES:
        entry = collections[lane]
        if not isinstance(entry, dict) \
                or not isinstance(entry.get("directory_present"), bool) \
                or not isinstance(entry.get("files"), dict):
            return None, "collections_invalid"
        for name, digest in entry["files"].items():
            if not isinstance(name, str) or "/" in name or "\x00" in name \
                    or not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                return None, "collections_invalid"
    document[_LOADED_FROM_KEY] = str(resolved)
    return document, None


def derived_root(context: dict) -> Path | None:
    loaded_from = context.get(_LOADED_FROM_KEY)
    if not isinstance(loaded_from, str):
        return None
    parent = Path(loaded_from).parent
    if tuple(parent.parts[-len(PROTECTED_SUBDIR_PARTS):]) != PROTECTED_SUBDIR_PARTS:
        return None
    return Path(*parent.parts[:-len(PROTECTED_SUBDIR_PARTS)])


def enforcement_required(context: dict | None, environ: Any = None) -> bool:
    """True when the WI-2 denies must fire: the runner-owned context says "enforced"
    (Pack-2), or the runner exported TRADING_RUN_CONTEXT_REQUIRED=1 (fail-closed even
    when the context file itself is missing/invalid)."""
    env = os.environ if environ is None else environ
    if env.get(ENV_REQUIRED) == "1":
        return True
    return bool(context) and context.get("enforcement") == ENFORCEMENT_ENFORCED


def binding_errors(context: dict, *, environ: Any = None, lane: str,
                   receipt_filename: str) -> list[str]:
    """Deny reasons binding env + the on-disk receipt to the runner-owned context.
    Empty list == the claimed receipt is byte-identical to what the runner inventoried
    BEFORE the model child launched, and env agrees with the runner's own context."""
    env = os.environ if environ is None else environ
    errors: list[str] = []
    if lane not in LANES:
        return ["lane_invalid"]
    root = derived_root(context)
    if root is None:
        return ["context_root_underivable"]
    target = context["target_session_date"]
    research = root / "data" / "runtime" / "research" / target
    env_target = env.get(ENV_TARGETS[0]) or env.get(ENV_TARGETS[1])
    if env_target and str(env_target) != target:
        errors.append("target_env_mismatch")
    env_dir = env.get(ENV_RESEARCH_DIR)
    if env_dir:
        try:
            if Path(os.path.realpath(env_dir)) != Path(os.path.realpath(research)):
                errors.append("research_dir_env_mismatch")
        except (OSError, ValueError):
            errors.append("research_dir_env_mismatch")
    inventory = context["collections"][lane]["files"]
    subdir = research / LANES[lane]["subdir"]
    if receipt_filename not in inventory:
        errors.append("receipt_not_in_runner_inventory")
    else:
        current = _sha_file(subdir / receipt_filename)
        if current is None:
            errors.append("receipt_missing_on_disk")
        elif current != inventory[receipt_filename]:
            errors.append("receipt_bytes_differ_from_runner_inventory")
    collection_name = LANES[lane]["collection"]
    if collection_name not in inventory:
        errors.append("collection_seal_not_in_runner_inventory")
    else:
        current = _sha_file(subdir / collection_name)
        if current is None or current != inventory[collection_name]:
            errors.append("collection_seal_differs_from_runner_inventory")
    return errors


def _import_manifest_module():
    try:
        import source_receipt_manifest  # noqa: PLC0415
        return source_receipt_manifest
    except ImportError:
        candidate = str(Path(__file__).resolve().parent.parent)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        import source_receipt_manifest  # noqa: PLC0415
        return source_receipt_manifest


def manifest_errors(context: dict, *, lane: str, accession: str | None = None,
                    receipt_id: str | None = None) -> list[str]:
    """Deny reasons from the shadow source-receipt manifest for THIS (target, run id):
    the published manifest must exist, revalidate (byte-canonical + full re-derivation
    against current disk), and inventory the claimed receipt with all three verdicts
    true.  Empty list == manifest inventory confirms the receipt."""
    if lane not in LANES:
        return ["lane_invalid"]
    try:
        srm = _import_manifest_module()
    except ImportError:
        return ["manifest_module_unavailable"]
    root = derived_root(context)
    if root is None:
        return ["context_root_underivable"]
    target = context["target_session_date"]
    run_id = context["audited_run_id"]
    try:
        policy, _ = srm._load_policy(root)  # noqa: SLF001 — established cross-module use
        relative = srm._manifest_rel(policy, target, run_id)  # noqa: SLF001
        raw = srm._stable_read_rel(  # noqa: SLF001
            root, relative, policy["limits"]["maximum_manifest_bytes"],
            required_mode=0o600)
        document = srm._validate_manifest_raw(  # noqa: SLF001
            raw=raw, root=root, policy=policy, target=target,
            audited_run_id=run_id, expected_path=relative)
    except srm.ManifestError as exc:
        return [f"manifest_invalid:{str(exc)[:160]}"]
    except Exception:
        return ["manifest_invalid:unexpected_error"]
    units = (((document.get("lanes") or {}).get(lane) or {}).get("units")) or []
    if lane == "sec":
        matching = [u for u in units if isinstance(u, dict)
                    and u.get("accession_number") == accession]
    else:
        expected_name = f"{receipt_id}.json"
        matching = [u for u in units if isinstance(u, dict)
                    and isinstance(u.get("expected_receipt_path"), str)
                    and u["expected_receipt_path"].rsplit("/", 1)[-1] == expected_name]
    if not matching:
        return ["manifest_unit_missing"]
    for unit in matching:
        if (unit.get("receipt_file_present") is True
                and unit.get("receipt_contract_valid") is True
                and unit.get("receipt_unit_identity_matched") is True):
            return []
    return ["manifest_unit_verdicts_incomplete"]


# ------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mint", help="print a fresh runner-private run nonce")
    write = commands.add_parser("write", help="write the runner-owned run-context")
    write.add_argument("--root", type=Path, required=True)
    write.add_argument("--target", required=True)
    write.add_argument("--audited-run-id", required=True)
    write.add_argument("--run-nonce", required=True)
    write.add_argument("--enforcement", default=ENFORCEMENT_SHADOW,
                       choices=list(ENFORCEMENT_VALUES))
    args = parser.parse_args(argv)
    if args.command == "mint":
        print(mint_nonce())
        return 0
    try:
        destination = write_context(
            args.root, target=args.target, audited_run_id=args.audited_run_id,
            run_nonce=args.run_nonce, enforcement=args.enforcement)
    except (ValueError, OSError) as exc:
        print(f"run_context_write_failed:{exc}", file=sys.stderr)
        return 2
    print(str(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
