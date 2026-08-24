#!/usr/bin/env python3
"""In-memory control-plane envelope for every unattended model child.

The model process may create research artifacts, but it must never change the code, prompts,
configuration, hooks, manifests, or constitutions that decide whether those artifacts are
authoritative.  This module snapshots that control plane before the child starts, compares it
after exit, preserves any changed bytes in a private quarantine, restores the exact pre-child
state, and removes authority-bearing outputs created by a compromised invocation.

No broker, model, network, credential, or order surface exists here.  All paths are local.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "control_plane_integrity_receipt/1.0"
CONTROL_TREES = (
    "scripts",
    "config",
    "prompts",
    "schemas",
    "launchd",
    ".claude",
    ".codex",
    # The model never owns the outer publisher's persistent flock namespace. Protecting the
    # directory also prevents a detached descendant from pre-creating/holding a split-brain lock.
    "data/runtime/research/.publisher_locks",
)
CONTROL_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "data/MANIFEST.json",
    "data/trial_baseline_manifest.json",
    "data/broker_tool_namespace.json",
    "data/fractional_capability.json",
    "data/model_inventory.json",
    "data/quant_strategy_registry.jsonl",
    "data/research_ledger.json",
    # flock state is part of the append transaction even though its bytes carry no research data.
    "data/research_ledger.lock",
    "data/run_diagnostics.json",
    "data/shadow_ledger.json",
    "data/system_state.json",
)
# These entrypoints belong exclusively to the unsandboxed outer controller.  An audited model
# child has no legitimate reason to read or execute them: its prompt and allowed artifact paths
# are prepared before launch.  Denying reads is essential for interpreted scripts because a
# process-exec rule alone applies to the interpreter rather than necessarily to the script bytes.
OUTER_ONLY_CHILD_ENTRYPOINTS = (
    "scripts/audited_claude.py",
    "scripts/run_candidate_quote_hydrator.sh",
    "scripts/candidate_quote_barrier.py",
    "scripts/run_robinhood_readonly_collector.sh",
    "scripts/lib/run_lock.py",
)
# Python bytecode caches can otherwise provide a second readable copy of Python entrypoints even
# when their source paths are denied. Derive every cache parent from the source list so a nested
# outer-only module (for example scripts/lib/run_lock.py) cannot be added without its cache tree
# inheriting the same read denial.
OUTER_ONLY_CHILD_READ_TREES = tuple(
    sorted({
        (Path(relative).parent / "__pycache__").as_posix()
        for relative in OUTER_ONLY_CHILD_ENTRYPOINTS
        if Path(relative).suffix == ".py"
    })
)
# Dynamic outer-controller state must be model-immutable during the child lifetime, but must NOT
# enter the persistent control-plane snapshot/digest: the owner legitimately creates and removes
# these paths around a run, and later receipt revalidation occurs after that lifecycle completes.
MODEL_IMMUTABLE_RUNTIME_TREES = (
    ".run.lock",
    "data/runtime/audited_window",
)
# Volatile control-plane files that sibling automation jobs (watchdog/intake heartbeat via
# record_state) legitimately rewrite mid-run. They REMAIN in CONTROL_FILES so the audited
# child stays OS-sandboxed away from them (the Seatbelt deny-list is built from CONTROL_FILES),
# which is exactly why any in-window change is provably a benign concurrent sibling write and
# not a child tamper; they are therefore excluded from the envelope violation comparison only.
COMPARISON_EXEMPT_CONTROL_FILES = frozenset({"data/system_state.json"})
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".tmp"}


class IntegrityError(RuntimeError):
    """The control-plane envelope could not be established or restored safely."""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return _sha_bytes(encoded)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _ignored(relative: Path) -> bool:
    return (any(part in IGNORED_PARTS for part in relative.parts)
            or relative.suffix in IGNORED_SUFFIXES)


def _state(path: Path, *, keep_bytes: bool) -> tuple[dict, bytes | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}, None
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return {"kind": "symlink", "target": os.readlink(path), "mode": mode}, None
    if stat.S_ISDIR(metadata.st_mode):
        return {"kind": "directory", "mode": mode}, None
    if not stat.S_ISREG(metadata.st_mode):
        return {"kind": "special", "mode": mode}, None
    try:
        data = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise IntegrityError(f"control_plane_file_unreadable:{path.name}") from exc
    if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
    ) != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
    ):
        raise IntegrityError(f"control_plane_file_changed_during_snapshot:{path.name}")
    projection = {
        "kind": "regular",
        "mode": mode,
        # A second directory entry is an alternate write path that bypasses path-based policy.
        # Keep the count in the compared projection so aliases created after preflight are drift.
        "nlink": metadata.st_nlink,
        "size": len(data),
        "sha256": _sha_bytes(data),
    }
    return projection, data if keep_bytes else None


def _tree_entries(root: Path, relative_root: Path) -> list[tuple[str, Path]]:
    base = root / relative_root
    entries = [(relative_root.as_posix() + "/", base)]
    if not base.is_dir() or base.is_symlink():
        return entries
    for path in sorted(base.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        if path.is_dir() and not path.is_symlink():
            entries.append((relative.as_posix() + "/", path))
            continue
        entries.append((relative.as_posix(), path))
    return entries


def capture_control_plane(root: Path, *, settings_path: Path | None = None,
                          reject_unsafe: bool = True,
                          extra_control_files: tuple[str, ...] = ()) -> dict:
    """Capture a serializable projection plus private pre-child bytes for restoration."""
    root = root.resolve()
    projection: dict[str, dict] = {}
    contents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for tree in CONTROL_TREES:
        for label, path in _tree_entries(root, Path(tree)):
            state_doc, data = _state(path, keep_bytes=True)
            if reject_unsafe and state_doc["kind"] in {"symlink", "special"}:
                raise IntegrityError(f"control_plane_symlink_forbidden:{label}")
            if reject_unsafe and state_doc["kind"] == "regular" \
                    and state_doc.get("nlink") != 1:
                raise IntegrityError(f"control_plane_hardlink_forbidden:{label}")
            projection[label] = state_doc
            paths[label] = path
            if data is not None:
                contents[label] = data
    control_files = tuple(dict.fromkeys((*CONTROL_FILES, *extra_control_files)))
    for relative in control_files:
        path = root / relative
        state_doc, data = _state(path, keep_bytes=True)
        if reject_unsafe and state_doc["kind"] in {"symlink", "special"}:
            raise IntegrityError(f"control_plane_symlink_forbidden:{relative}")
        if reject_unsafe and state_doc["kind"] == "regular" \
                and state_doc.get("nlink") != 1:
            raise IntegrityError(f"control_plane_hardlink_forbidden:{relative}")
        projection[relative] = state_doc
        paths[relative] = path
        if data is not None:
            contents[relative] = data
    if settings_path is not None:
        settings = Path(os.path.abspath(os.fspath(settings_path)))
        if settings not in paths.values():
            label = "@audited-settings"
            state_doc, data = _state(settings, keep_bytes=True)
            if reject_unsafe and state_doc["kind"] != "regular":
                raise IntegrityError("audited_settings_not_regular")
            if reject_unsafe and state_doc.get("nlink") != 1:
                raise IntegrityError("audited_settings_hardlink_forbidden")
            projection[label] = state_doc
            paths[label] = settings
            if data is not None:
                contents[label] = data
    return {
        "root": root,
        "projection": projection,
        # Full-surface digest: forensics and restoration reasoning only.
        "snapshot_sha256": _canonical_hash(projection),
        # Compared-surface digest: the ONLY digest an integrity receipt may publish, so a
        # receipt can never claim control_plane_unchanged=true while showing before != after.
        "comparison_sha256": _canonical_hash(_comparison_projection(projection)),
        "contents": contents,
        "paths": paths,
        "extra_control_files": tuple(extra_control_files),
    }


def _comparison_projection(projection: dict) -> dict:
    """The control-plane surface the violation comparison actually judges.

    Exempt files are volatile sibling-owned heartbeats (see COMPARISON_EXEMPT_CONTROL_FILES).
    Excluding them here -- and nowhere else -- keeps the published envelope digests describing
    exactly the same surface as ``ok`` and ``changed_paths``.
    """
    return {
        label: state
        for label, state in projection.items()
        if label not in COMPARISON_EXEMPT_CONTROL_FILES
    }


def comparison_digest(snapshot: dict) -> str:
    """Return the envelope digest over the compared (exempt-filtered) projection."""
    return _canonical_hash(_comparison_projection(snapshot["projection"]))


def compare_control_plane(before: dict, *, settings_path: Path | None = None) -> dict:
    after = capture_control_plane(
        before["root"], settings_path=settings_path, reject_unsafe=False,
        extra_control_files=tuple(before.get("extra_control_files") or ()))
    labels = sorted(set(before["projection"]) | set(after["projection"]))
    changed = [label for label in labels
               if before["projection"].get(label) != after["projection"].get(label)
               and label not in COMPARISON_EXEMPT_CONTROL_FILES]
    exempt_changed = [label for label in labels
                      if before["projection"].get(label) != after["projection"].get(label)
                      and label in COMPARISON_EXEMPT_CONTROL_FILES]
    return {
        "ok": not changed,
        # Digests span the compared surface, so before == after exactly when ok is True.
        "before_sha256": comparison_digest(before),
        "after_sha256": comparison_digest(after),
        "changed_paths": changed,
        "changed_count": len(changed),
        # Benign sibling heartbeat writes remain available for diagnosis without becoming a
        # violation or leaking into the receipt's compared-surface digest.
        "exempt_changed_paths": exempt_changed,
        "before_full_sha256": before["snapshot_sha256"],
        "after_full_sha256": after["snapshot_sha256"],
        "after": after,
    }


def _authority_paths(root: Path, target: str) -> set[Path]:
    paths: set[Path] = set()
    exact = (
        root / "reports" / "morning" / f"{target}.md",
        root / "reports" / "morning" / "official_research" / f"{target}.md",
        root / "reports" / "intraday" / f"{target}-postopen.md",
        root / "reports" / "post_close" / f"{target}.md",
        root / "data" / "runtime" / "research" / target / "freeze_receipt.json",
        root / "data" / "runtime" / "natural_run_acceptance" / "current.json",
        root / "data" / "research_ledger.json",
        root / "data" / "run_diagnostics.json",
        root / "data" / "shadow_ledger.json",
        root / "data" / "system_state.json",
        root / "data" / "watchlist.json",
    )
    paths.update(exact)
    preliminary = root / "reports" / "morning" / "preliminary"
    if preliminary.is_dir() and not preliminary.is_symlink():
        paths.update(preliminary.glob(f"{target}_*.md"))
    deterministic = (root / "data" / "runtime" / "research" / target
                     / "deterministic_terminals")
    if deterministic.is_dir() and not deterministic.is_symlink():
        paths.update(deterministic.glob("*.json"))
    runtime = root / "data" / "runtime"
    if runtime.is_dir() and not runtime.is_symlink():
        for path in runtime.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            try:
                relative = path.relative_to(runtime)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in {
                    "broker_audit", "control_plane_quarantine"}:
                continue
            # Sibling autonomy jobs (watchdog) own their date-stamped monitoring artifacts;
            # they are not Morning-child authority outputs and the watchdog legitimately
            # rewrites them mid-window, so they must not enter the child authority surface.
            # Every other date-stamped runtime path stays protected.
            if relative.name.endswith("_autonomy_service_health.json"):
                continue
            rendered = relative.as_posix().lower()
            if target in rendered:
                paths.add(path)
    return paths


def capture_authority_surface(root: Path, target: str, *, reject_unsafe: bool = True) -> dict[str, dict]:
    root = root.resolve()
    result: dict[str, dict] = {}
    for path in sorted(_authority_paths(root, target), key=lambda value: value.as_posix()):
        try:
            relative = path.absolute().relative_to(root.absolute()).as_posix()
        except ValueError as exc:
            raise IntegrityError("authority_path_outside_root") from exc
        state_doc, data = _state(path, keep_bytes=True)
        if reject_unsafe and state_doc["kind"] in {"symlink", "special"}:
            raise IntegrityError(f"authority_surface_symlink_forbidden:{relative}")
        result[relative] = {"state": state_doc, "bytes": data}
    return result


def _surface_projection(surface: dict[str, dict]) -> dict[str, dict]:
    return {path: value["state"] for path, value in surface.items()}


def compare_authority_surface(before: dict[str, dict], root: Path, target: str) -> dict:
    """Compare authority-bearing bytes without mutating them.

    The audited wrapper uses this on successful Morning children to enforce a lane allowlist;
    failed/violating children still use :func:`quarantine_authority_changes` for restoration.
    """
    after = capture_authority_surface(root, target, reject_unsafe=False)
    before_projection = _surface_projection(before)
    after_projection = _surface_projection(after)
    labels = sorted(set(before_projection) | set(after_projection))
    changed = [label for label in labels
               if before_projection.get(label) != after_projection.get(label)]
    return {
        "ok": not changed,
        "changed_paths": changed,
        "changed_count": len(changed),
        "after": after,
    }


def reserve_quarantine(root: Path, target: str, invocation_id: str) -> Path:
    base = root / "data" / "runtime" / "control_plane_quarantine" / target
    base.mkdir(parents=True, exist_ok=True)
    destination = base / invocation_id
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise IntegrityError("control_plane_quarantine_collision") from exc
    return destination


def _move_current(path: Path, destination: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise IntegrityError("control_plane_quarantine_destination_collision")
    os.replace(path, destination)
    return True


def _restore_regular(path: Path, before: dict, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data, int(before.get("mode", 0o600)))


def restore_control_plane(before: dict, comparison: dict, quarantine: Path,
                          *, settings_path: Path | None = None) -> dict:
    """Preserve changed child-time bytes, restore pre-child files, then re-verify exactly."""
    after = comparison["after"]
    moved: list[str] = []
    restored: list[str] = []
    errors: list[str] = []
    structure_labels = [label for label in comparison["changed_paths"] if label.endswith("/")]
    # Repair a whole control-tree root that was deleted or replaced by a file/symlink before
    # restoring its children. A newly-created real directory is removed only after its files.
    for label in sorted(structure_labels, key=len):
        path = after["paths"].get(label) or before["paths"].get(label)
        if path is None:
            errors.append(f"path_mapping_missing:{label}")
            continue
        prior = before["projection"].get(label, {"kind": "missing"})
        current = after["projection"].get(label, {"kind": "missing"})
        try:
            if prior.get("kind") == "directory" and current.get("kind") != "directory":
                if _move_current(path, quarantine / "control_plane" / Path(label.rstrip("/"))):
                    moved.append(label)
                path.mkdir(parents=True, exist_ok=True)
                os.chmod(path, int(prior.get("mode", 0o755)))
                restored.append(label)
            elif prior.get("kind") == "directory" and current.get("kind") == "directory":
                os.chmod(path, int(prior.get("mode", 0o755)))
            elif prior.get("kind") == "missing" and current.get("kind") not in {
                    "missing", "directory"}:
                if _move_current(path, quarantine / "control_plane" / Path(label.rstrip("/"))):
                    moved.append(label)
        except Exception as exc:
            errors.append(f"{label}:{type(exc).__name__}")

    file_labels = [label for label in comparison["changed_paths"] if not label.endswith("/")]
    for label in file_labels:
        path = after["paths"].get(label) or before["paths"].get(label)
        if path is None:
            errors.append(f"path_mapping_missing:{label}")
            continue
        safe_label = (Path("external") / "audited-settings"
                      if label == "@audited-settings" else Path(label))
        try:
            if _move_current(path, quarantine / "control_plane" / safe_label):
                moved.append(label)
            prior = before["projection"].get(label, {"kind": "missing"})
            if prior.get("kind") == "regular":
                data = before["contents"].get(label)
                if data is None:
                    raise IntegrityError("pre_child_bytes_missing")
                _restore_regular(path, prior, data)
                restored.append(label)
            elif prior.get("kind") not in {"missing"}:
                raise IntegrityError("unsupported_pre_child_control_state")
        except Exception as exc:
            errors.append(f"{label}:{type(exc).__name__}")

    # Remove only newly-created empty control directories. Never recursively delete.
    for label in sorted(structure_labels, key=len, reverse=True):
        if before["projection"].get(label, {}).get("kind") == "missing":
            path = before["paths"].get(label) or after["paths"].get(label)
            try:
                if path is not None:
                    path.rmdir()
            except OSError:
                pass
    try:
        verified = compare_control_plane(before, settings_path=settings_path)
        exact = verified["ok"]
        remaining = verified["changed_paths"]
    except Exception as exc:
        exact = False
        remaining = [f"reverify_error:{type(exc).__name__}"]
    return {
        "restored_exactly": exact,
        "quarantined_control_paths": moved,
        "restored_control_paths": restored,
        "remaining_drift": remaining,
        "errors": errors,
    }


def quarantine_authority_changes(before: dict[str, dict], root: Path, target: str,
                                 quarantine: Path) -> dict:
    """Quarantine only authority-bearing outputs changed during the compromised child."""
    root = root.resolve()
    after = capture_authority_surface(root, target, reject_unsafe=False)
    before_projection = _surface_projection(before)
    after_projection = _surface_projection(after)
    labels = sorted(set(before_projection) | set(after_projection))
    changed = [label for label in labels
               if before_projection.get(label) != after_projection.get(label)]
    quarantined: list[str] = []
    restored: list[str] = []
    errors: list[str] = []
    for label in changed:
        path = root / label
        try:
            if _move_current(path, quarantine / "authority_outputs" / label):
                quarantined.append(label)
            prior = before.get(label)
            if prior and prior["state"].get("kind") == "regular":
                data = prior.get("bytes")
                if data is None:
                    raise IntegrityError("pre_child_authority_bytes_missing")
                _restore_regular(path, prior["state"], data)
                restored.append(label)
        except Exception as exc:
            errors.append(f"{label}:{type(exc).__name__}")
    try:
        verified = capture_authority_surface(root, target)
        remaining = sorted(
            label for label in set(before_projection) | set(_surface_projection(verified))
            if before_projection.get(label) != _surface_projection(verified).get(label))
    except Exception as exc:
        remaining = [f"reverify_error:{type(exc).__name__}"]
    return {
        "changed_authority_paths": changed,
        "quarantined_authority_paths": quarantined,
        "restored_authority_paths": restored,
        "restored_exactly": not remaining,
        "remaining_drift": remaining,
        "errors": errors,
    }


def write_receipt(path: Path, *, target: str, workflow: str, run_id: str,
                  invocation_id: str, comparison: dict, restoration: dict | None,
                  authority: dict | None, broker_violation: bool,
                  unauthorized_authority_paths: list[str] | None,
                  generated_utc: str) -> dict:
    document = {
        "schema": SCHEMA,
        "target_session_date": target,
        "workflow": workflow,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "generated_utc": generated_utc,
        "control_plane_unchanged": comparison.get("ok") is True,
        "before_control_plane_sha256": comparison.get("before_sha256"),
        "after_control_plane_sha256": comparison.get("after_sha256"),
        "changed_control_paths": list(comparison.get("changed_paths") or []),
        "control_plane_restoration": restoration,
        "broker_violation_detected": bool(broker_violation),
        "authority_output_quarantine": authority,
        "unauthorized_authority_paths": sorted(set(
            unauthorized_authority_paths or [])),
        "execution_enabled": False,
        "activation_authority": False,
    }
    document["receipt_sha256"] = _canonical_hash(document)
    _atomic_write(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
                  0o600, create_only=True)
    return document
