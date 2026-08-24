#!/usr/bin/env python3
"""Launch one Claude CLI process with mandatory broker-attempt audit hooks.

The child stdout/stderr and normal exit code are preserved. Any observed broker preview/write or
unknown broker-shaped tool attempt overrides the result with exit 86. Missing heartbeat coverage
is reported as unobservable; it blocks only when `--require-coverage` is explicitly enabled during
the later promotion stage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import broker_runtime_audit as audit  # noqa: E402
import control_plane_integrity as integrity  # noqa: E402
import secrets_loader  # noqa: E402
import audited_window  # noqa: E402

FORBIDDEN_ARGS = {"--dangerously-skip-permissions", "--settings"}
SAFE_LANE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
WRITE_ONLY_PATH_ENV = "TRADING_AUDITED_WRITE_ONLY_PATH"
AUDITED_CHILD_ENV = "TRADING_AUDITED_MODEL_CHILD"
CONTROL_PLANE_VIOLATION_EXIT = 88
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
ACTIVE_CHILD: subprocess.Popen | None = None
SIGNAL_COUNT = 0
TERMINATION_SIGNAL: int | None = None


class TerminationRequested(BaseException):
    """Unwind a pre-launch signal without manufacturing an audited child result."""

    def __init__(self, signum: int):
        super().__init__(f"termination_requested:{signum}")
        self.signum = int(signum)
        self.exit_code = 128 + abs(self.signum)


def _invocation_id(lane: str, now: dt.datetime | None = None) -> str:
    if not SAFE_LANE_RE.fullmatch(lane):
        raise audit.AuditError("lane_invalid")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    stamp = current.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{lane}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"


def _validate_child_args(arguments: list[str]) -> None:
    if not arguments:
        raise audit.AuditError("claude_arguments_missing")
    for index, value in enumerate(arguments):
        if value in FORBIDDEN_ARGS or any(value.startswith(item + "=") for item in FORBIDDEN_ARGS):
            raise audit.AuditError("caller_may_not_override_audit_or_permissions")
        if value == "--permission-mode" and index + 1 < len(arguments) \
                and arguments[index + 1] == "bypassPermissions":
            raise audit.AuditError("bypass_permissions_forbidden")
        if value.startswith("--permission-mode=") \
                and value.split("=", 1)[1] == "bypassPermissions":
            raise audit.AuditError("bypass_permissions_forbidden")


def _validated_write_scope(*, root: Path, target: str, lane: str,
                           write_only_path: Path | None) -> Path | None:
    """Return one child-visible write path or fail closed for write-scoped lanes."""
    if lane == "candidate-quotes" and write_only_path is None:
        raise audit.AuditError("candidate_quote_write_scope_required")
    if write_only_path is None:
        return None
    resolved = write_only_path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise audit.AuditError("write_scope_outside_project") from exc
    if lane == "candidate-quotes":
        expected_parent = (root / "data" / "runtime" / "rh_candidate_quote_raw").resolve()
        if resolved.parent != expected_parent:
            raise audit.AuditError("candidate_quote_write_scope_parent_invalid")
        pattern = re.compile(rf"^raw_{re.escape(target)}_[A-Za-z0-9._-]+\.json$")
        if not pattern.fullmatch(resolved.name):
            raise audit.AuditError("candidate_quote_write_scope_name_invalid")
    return resolved


def _terminate_child(signum: int, _frame) -> None:
    global SIGNAL_COUNT, TERMINATION_SIGNAL
    SIGNAL_COUNT += 1
    TERMINATION_SIGNAL = int(signum)
    child = ACTIVE_CHILD
    if child is None:
        # BaseException prevents broad Exception handlers in comparison/finalization code from
        # swallowing an operator cancellation. run_audited's finally still closes the marker.
        raise TerminationRequested(signum)
    try:
        # The outer shell timeout kills only this wrapper PID after a short grace. Kill the entire
        # child session immediately so an MCP/model descendant cannot outlive an audit receipt.
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_remaining_child_process_group(process_group_id: int) -> None:
    """Best-effort cleanup for descendants left behind after the group leader exits.

    ``Popen.wait()`` reaps only the direct child. Because that child starts a new session, its PID
    is also the process-group ID and remains a valid kill target while any inherited descendant is
    alive, even after the direct child has returned. Cleanup must precede integrity comparison so a
    delayed descendant cannot mutate authority-bearing state after the wrapper reports success.
    """
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        # ESRCH is the normal no-descendants case. Other failures are intentionally best-effort:
        # audit/finality still proceeds, while the mandatory OS write guard remains inherited by
        # production descendants.
        pass


def _block_termination_signals() -> set[signal.Signals]:
    """Start one small launch/marker critical section or fail closed."""
    try:
        return signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    except (AttributeError, OSError) as exc:
        raise audit.AuditError("termination_signal_mask_unavailable") from exc


def _restore_signal_mask(previous: set[signal.Signals]) -> None:
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _seatbelt_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _lane_control_files(lane: str, target: str) -> tuple[str, ...]:
    """Return mutable authority files that this exact model lane must treat as read-only."""
    protected: list[str] = []
    # Only the two post-close narrative lanes may propose a watchlist update, which is later bound
    # by the deterministic report contract/finality receipt. Every other lane is read-only here.
    if lane not in {"postclose-main", "postclose-contract-repair"}:
        protected.append("data/watchlist.json")
    if lane == "morning-main":
        # The model writes only draft.md, candidates_draft.json, and freeze_request.json. These
        # publisher-owned outputs must not exist until the trusted outer runner takes over.
        protected.extend((
            f"data/runtime/preflight/{target}.json",
            f"data/runtime/macro_actual_refresh/{target}.json",
            f"data/runtime/fmp_enrichment/{target}.json",
            f"data/runtime/robinhood/snapshot_{target}.json",
            f"data/runtime/webull/{target}/snapshot.json",
            f"data/runtime/webull/{target}/receipt.json",
            f"data/runtime/asset_allocation/{target}/context.json",
            f"data/runtime/research/{target}/sec_candidate_seed.json",
            f"data/runtime/research/{target}/sec_review_shadow/queue.json",
            f"data/runtime/research/{target}/sec_review_shadow/result.json",
            f"data/runtime/research/{target}/freeze_receipt.json",
            f"data/runtime/research/{target}/candidates.json",
        ))
    return tuple(protected)


def _lane_sibling_owned_files(lane: str, target: str) -> tuple[str, ...]:
    """Dynamic outer-sibling outputs the model must not write.

    These paths deliberately do *not* enter the wrapper's before/after control-plane digest:
    the candidate-quote sibling is expected to create them while the morning model is alive.
    They do enter the inherited Seatbelt deny-write profile, which distinguishes the model and
    its descendants from that unsandboxed outer sibling.
    """
    if lane != "morning-main":
        return ()
    return (
        f"data/runtime/robinhood/candidate_quote_request_{target}.json",
        f"data/runtime/robinhood/candidate_quotes_{target}.json",
        f"data/runtime/health/{target}_candidate_quote_health.json",
    )


def _lane_os_guard_files(lane: str, target: str) -> tuple[str, ...]:
    """Every file denied to this lane by the OS write guard."""
    return tuple(dict.fromkeys((
        *_lane_control_files(lane, target),
        *_lane_sibling_owned_files(lane, target),
    )))


def _lane_control_trees(lane: str) -> tuple[str, ...]:
    if lane != "morning-main":
        return ()
    return (
        "reports",
        "data/runtime/data_lake",
        "data/runtime/free_source_ensemble",
        "data/runtime/free_source_health",
        "data/runtime/security_master_rollforward",
        "data/runtime/post_open_market_snapshot",
        "data/runtime/post_close_market_snapshot",
        "data/runtime/natural_run_acceptance",
    )


def _morning_authority_change_allowed(path: str, target: str) -> bool:
    """Final-state outputs a successful Morning model lane is allowed to create/change."""
    research_prefix = f"data/runtime/research/{target}/"
    if path.startswith(research_prefix):
        return True
    return path in {
        # Sibling-owned control-plane heartbeat: the Morning child is OS-sandboxed away from
        # data/system_state.json (Seatbelt deny-list), so an in-window change is a benign
        # concurrent record_state() write, never a child edit.
        "data/system_state.json",
        f"data/runtime/robinhood/candidate_quote_request_{target}.json",
        f"data/runtime/robinhood/candidate_quotes_{target}.json",
        f"data/runtime/health/{target}_candidate_quote_health.json",
        f"data/runtime/web_verification/{target}.json",
    }


def _control_write_guard_profile(root: Path, settings_path: Path,
                                 *, extra_control_files: tuple[str, ...] = (),
                                 extra_control_trees: tuple[str, ...] = ()) -> str:
    """Build a deny-only macOS Seatbelt profile for the audited-child boundary.

    The hook layer remains useful feedback, but it parses command text and cannot be the security
    boundary for dynamically constructed paths. This OS policy is inherited by every descendant
    and blocks even transient write-then-restore mutations while leaving research outputs writable.
    It also makes outer-controller entrypoints and their bytecode cache unreadable/unexecutable
    inside the child, without restricting the unsandboxed parent controller.
    """
    root = root.resolve()
    protected_trees = [
        root / relative
        for relative in (
            *integrity.CONTROL_TREES,
            *integrity.MODEL_IMMUTABLE_RUNTIME_TREES,
            *extra_control_trees,
        )
    ]
    protected_files = [
        root / relative
        for relative in (*integrity.CONTROL_FILES, *extra_control_files)
    ]
    resolved_settings = settings_path.resolve()
    if resolved_settings not in protected_files:
        protected_files.append(resolved_settings)
    lines = [
        "(version 1)",
        "(allow default)",
        # Path-only write denies are insufficient if a child first creates an allowed hardlink to
        # a protected inode. Block the file-link operation globally; ordinary file creation stays
        # allowed and the rule is inherited by every descendant.
        "(deny file-link)",
    ]
    for path in sorted(set(protected_trees), key=lambda value: str(value)):
        quoted = _seatbelt_quote(path)
        lines.append(f'(deny file-write* (literal "{quoted}"))')
        lines.append(f'(deny file-write* (subpath "{quoted}"))')
    for path in sorted(set(protected_files), key=lambda value: str(value)):
        lines.append(f'(deny file-write* (literal "{_seatbelt_quote(path)}"))')
    for relative in integrity.OUTER_ONLY_CHILD_ENTRYPOINTS:
        path = root / relative
        quoted = _seatbelt_quote(path)
        lines.append(f'(deny file-read* (literal "{quoted}"))')
        lines.append(f'(deny process-exec (literal "{quoted}"))')
    for relative in integrity.OUTER_ONLY_CHILD_READ_TREES:
        path = root / relative
        quoted = _seatbelt_quote(path)
        lines.append(f'(deny file-read* (literal "{quoted}"))')
        lines.append(f'(deny file-read* (subpath "{quoted}"))')
    return "\n".join(lines) + "\n"


def _os_write_guard_mode(root: Path, settings_path: Path, *,
                         extra_control_files: tuple[str, ...] = (),
                         extra_control_trees: tuple[str, ...] = ()) -> tuple[str, str]:
    """Resolve how the control-plane OS write guard will be enforced for this invocation.

    Returns ("self_applied", detail) when sandbox-exec can wrap the child, or
    ("unavailable", <exact reason>) — the caller must then fail closed BEFORE opening the
    audited window or spawning anything, and must leave a terminal receipt.

    DELIBERATELY NO "inherited" MODE (2026-07-28 security review). A nested audited lane cannot
    apply its own Seatbelt profile (macOS sandbox_apply -> EPERM), and it is tempting to instead
    "prove" that an ancestor profile already denies everything by probing writes. That proof is
    unsound as a policy-equivalence claim: (a) a deny rule for a path that does not yet exist is
    a CREATE-deny, and a probe can only observe paths that exist, so most lane-specific extras
    (freeze_receipt.json, candidates.json, per-target snapshots) would be asserted denied without
    evidence; and (b) PermissionError cannot distinguish Seatbelt EPERM from ordinary filesystem
    EACCES, so plain permission hardening, a foreign uid, an ACL, or chflags uchg would each
    manufacture a false proof with no sandbox active at all. Failing the nested lane closed at
    rc=87 WITH a complete terminal receipt is the correct, honest outcome; a sound implementation
    would require a cryptographic parent->child profile handoff plus deny-set containment, not a
    behavioural probe.
    """
    if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        return "unavailable", "sandbox_exec_missing"
    try:
        probe = subprocess.run(
            [str(SANDBOX_EXEC), "-p", "(version 1)(allow default)", "/usr/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unavailable", f"sandbox_probe_error:{type(exc).__name__}"
    if probe.returncode != 0:
        return "unavailable", f"sandbox_probe_rc:{probe.returncode}"
    return "self_applied", "sandbox_exec_probe_ok"


def _guarded_child_command(command: list[str], *, root: Path,
                           settings_path: Path,
                           extra_control_files: tuple[str, ...] = (),
                           extra_control_trees: tuple[str, ...] = ()) -> list[str]:
    mode, detail = _os_write_guard_mode(
        root, settings_path, extra_control_files=extra_control_files,
        extra_control_trees=extra_control_trees)
    if mode != "self_applied":
        raise audit.AuditError(f"control_plane_os_write_guard_unavailable:{detail}")
    profile = _control_write_guard_profile(
        root, settings_path, extra_control_files=extra_control_files,
        extra_control_trees=extra_control_trees)
    return [str(SANDBOX_EXEC), "-p", profile, *command]


def _write_precondition_marker(path: Path, marker: dict) -> None:
    """Best-effort 0600 marker write; never raises into the caller's failure path."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        return


def _write_guard_unavailable_terminal(*, audit_root: Path, target: str, workflow: str,
                                      run_id: str, invocation_id: str, started: str,
                                      detail: str, now: dt.datetime | None = None) -> None:
    """Deterministic terminal evidence for a pre-child guard failure (never model-authored).

    Writes the canonical broker-audit receipt (child_exit_code=87, zero attempts, zero preview
    writes) plus a precondition-failure marker carrying the exact reason, so an attempted
    audited lane can never again disappear without terminal evidence (2026-07-28 incident).

    ``audit.start_invocation`` can ITSELF fail (invocation_artifact_collision, token/date
    validation, I/O). That was the one remaining path on which an attempted lane could still
    vanish silently, so the marker is also written to a receipt-independent fallback path
    derived only from audit_root/target/run_id (2026-07-28 review D3).

    Best-effort by design: the caller raises AuditError regardless. Statuses only — never
    account data.
    """
    finished = audit._utc(now)
    marker = {
        "schema": "broker_audit_precondition_failure/1.0",
        "status": "failed",
        "reason": f"control_plane_os_write_guard_unavailable:{detail}",
        "target_session_date": target,
        "workflow": workflow,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "started_utc": started,
        "finished_utc": finished,
        "child_spawned": False,
        "tool_calls": 0,
        "preview_writes": 0,
        "mutations": 0,
        "receipt_error": None,
        "safety": dict(audit.SAFETY),
    }
    try:
        context = audit.start_invocation(
            audit_root=audit_root, target=target, workflow=workflow,
            run_id=run_id, invocation_id=invocation_id, now=now)
    except Exception as exc:
        marker["receipt_error"] = f"start_invocation_failed:{type(exc).__name__}"
        _write_precondition_marker(
            Path(audit_root) / target / run_id
            / f"{invocation_id}.precondition_failure.json", marker)
        return
    try:
        audit.finalize_invocation(
            context, child_exit_code=87, started_utc=started, finished_utc=finished)
    except Exception as exc:
        marker["receipt_error"] = type(exc).__name__
    _write_precondition_marker(
        context["run_root"] / f"{invocation_id}.precondition_failure.json", marker)


def run_audited(*, root: Path, audit_root: Path, target: str, workflow: str,
                run_id: str, lane: str, claude: Path, arguments: list[str],
                settings_path: Path, require_coverage: bool = False,
                write_only_path: Path | None = None,
                now: dt.datetime | None = None,
                enforce_os_write_guard: bool | None = None) -> tuple[dict, int]:
    """Run one audited child and always close its outer audited-window marker."""
    window_marker: Path | None = None

    def remember_window_marker(path: Path) -> None:
        nonlocal window_marker
        window_marker = path

    previous_term = None
    previous_int = None
    try:
        # Cover the complete marker lifetime. A default SIGTERM does not unwind Python finally
        # blocks, so handlers must be installed before open_window can run and restored only after
        # close_window has completed.
        previous_term = signal.signal(signal.SIGTERM, _terminate_child)
        previous_int = signal.signal(signal.SIGINT, _terminate_child)
        return _run_audited_impl(
            root=root,
            audit_root=audit_root,
            target=target,
            workflow=workflow,
            run_id=run_id,
            lane=lane,
            claude=claude,
            arguments=arguments,
            settings_path=settings_path,
            require_coverage=require_coverage,
            write_only_path=write_only_path,
            now=now,
            enforce_os_write_guard=enforce_os_write_guard,
            remember_window_marker=remember_window_marker,
        )
    finally:
        audited_window.close_window(window_marker)
        if previous_int is not None:
            signal.signal(signal.SIGINT, previous_int)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def _expected_file_identity(path: Path, *, trusted_root: Path, label: str) -> tuple[int, ...]:
    """Return a race-sensitive identity for one trusted, single-link regular file."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise audit.AuditError(f"outer_only_entrypoint_unavailable:{label}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise audit.AuditError(f"outer_only_entrypoint_not_regular:{label}")
    if info.st_nlink != 1:
        raise audit.AuditError(f"outer_only_entrypoint_link_count_invalid:{label}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(trusted_root)
    except (OSError, ValueError) as exc:
        raise audit.AuditError(f"outer_only_entrypoint_path_invalid:{label}") from exc
    if resolved != path:
        raise audit.AuditError(f"outer_only_entrypoint_path_invalid:{label}")
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validated_outer_only_entrypoints() -> dict[str, tuple[int, ...]]:
    """Bind every outer-only source/cache file to the wrapper's trusted repository root."""
    try:
        trusted_root = ROOT.resolve(strict=True)
        scripts_dir = trusted_root / "scripts"
        scripts_info = scripts_dir.lstat()
    except OSError as exc:
        raise audit.AuditError("outer_only_trusted_root_unavailable") from exc
    if stat.S_ISLNK(scripts_info.st_mode) or not stat.S_ISDIR(scripts_info.st_mode) \
            or scripts_dir.resolve(strict=True) != scripts_dir:
        raise audit.AuditError("outer_only_trusted_root_invalid")

    identities: dict[str, tuple[int, ...]] = {}
    python_stems_by_parent: dict[Path, set[str]] = {}
    for relative in integrity.OUTER_ONLY_CHILD_ENTRYPOINTS:
        relative_path = Path(relative)
        if (relative_path.is_absolute() or ".." in relative_path.parts
                or relative_path.parts[:1] != ("scripts",)):
            raise audit.AuditError("outer_only_entrypoint_config_invalid")
        expected = trusted_root / relative_path
        identities[relative_path.as_posix()] = _expected_file_identity(
            expected, trusted_root=trusted_root, label=relative_path.as_posix())
        if expected.suffix == ".py":
            python_stems_by_parent.setdefault(expected.parent, set()).add(expected.stem)

    for source_parent, python_stems in sorted(
            python_stems_by_parent.items(), key=lambda item: str(item[0])):
        cache_dir = source_parent / "__pycache__"
        try:
            cache_info = cache_dir.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise audit.AuditError("outer_only_bytecode_cache_unavailable") from exc
        try:
            cache_dir.resolve(strict=True).relative_to(trusted_root)
        except (OSError, ValueError) as exc:
            raise audit.AuditError("outer_only_bytecode_cache_invalid") from exc
        if stat.S_ISLNK(cache_info.st_mode) or not stat.S_ISDIR(cache_info.st_mode) \
                or cache_dir.resolve(strict=True) != cache_dir:
            raise audit.AuditError("outer_only_bytecode_cache_invalid")
        try:
            cache_entries = sorted(cache_dir.iterdir(), key=lambda value: value.name)
        except OSError as exc:
            raise audit.AuditError("outer_only_bytecode_cache_unavailable") from exc
        for entry in cache_entries:
            if not entry.name.endswith(".pyc"):
                continue
            if not any(
                    entry.name == f"{stem}.pyc" or entry.name.startswith(f"{stem}.")
                    for stem in python_stems):
                continue
            label = entry.relative_to(trusted_root).as_posix()
            identities[label] = _expected_file_identity(
                entry, trusted_root=trusted_root, label=label)
    return identities


def _run_audited_impl(*, root: Path, audit_root: Path, target: str, workflow: str,
                      run_id: str, lane: str, claude: Path, arguments: list[str],
                      settings_path: Path, require_coverage: bool,
                      write_only_path: Path | None, now: dt.datetime | None,
                      enforce_os_write_guard: bool | None,
                      remember_window_marker) -> tuple[dict, int]:
    global ACTIVE_CHILD, SIGNAL_COUNT, TERMINATION_SIGNAL
    SIGNAL_COUNT = 0
    TERMINATION_SIGNAL = None
    if os.environ.get(AUDITED_CHILD_ENV) == "1":
        raise audit.AuditError("nested_audited_child_forbidden")
    trusted_entrypoint_identities = _validated_outer_only_entrypoints()
    root = root.resolve()
    claude = claude.expanduser().resolve()
    settings_path = settings_path.resolve()
    canonical_root = root == ROOT.resolve()
    if canonical_root and enforce_os_write_guard is False:
        raise audit.AuditError("canonical_control_plane_os_write_guard_may_not_be_disabled")
    if enforce_os_write_guard is None:
        # Production always uses the canonical root. Isolated temporary roots are hermetic tests
        # of the after-child restoration envelope and do not require a nested macOS sandbox.
        enforce_os_write_guard = canonical_root
    if not claude.is_file() or not os.access(claude, os.X_OK):
        raise audit.AuditError("claude_executable_invalid")
    if not settings_path.is_file() or audit._sha_file(settings_path) is None:
        raise audit.AuditError("audit_settings_unreadable")
    _validate_child_args(arguments)
    write_scope = _validated_write_scope(
        root=root, target=target, lane=lane, write_only_path=write_only_path)
    lane_control_files = _lane_control_files(lane, target)
    lane_os_guard_files = _lane_os_guard_files(lane, target)
    lane_control_trees = _lane_control_trees(lane)
    prior_sli_path = audit_root.resolve() / target / "sli.json"
    if prior_sli_path.is_file():
        try:
            prior_sli = json.loads(prior_sli_path.read_text(encoding="utf-8"))
        except Exception:
            prior_sli = None
        if isinstance(prior_sli, dict) and not audit.validate_sli(prior_sli) \
                and prior_sli.get("target_session_date") == target \
                and prior_sli.get("status") == "violation":
            raise audit.AuditViolation("prior_broker_runtime_violation_latched")
    invocation_id = _invocation_id(lane, now)
    started = audit._utc(now)
    # rc=87 hardening (2026-07-28): verify the OS write guard BEFORE the audited window opens
    # and before anything can spawn. A guard failure must never again strand an opened window
    # with no terminal receipt.
    guard_mode = "disabled"
    if enforce_os_write_guard:
        guard_mode, guard_detail = _os_write_guard_mode(
            root, settings_path,
            extra_control_files=lane_os_guard_files,
            extra_control_trees=lane_control_trees)
        if guard_mode == "unavailable":
            _write_guard_unavailable_terminal(
                audit_root=audit_root, target=target, workflow=workflow, run_id=run_id,
                invocation_id=invocation_id, started=started, detail=guard_detail, now=now)
            raise audit.AuditError(
                f"control_plane_os_write_guard_unavailable:{guard_detail}")
    try:
        control_before = integrity.capture_control_plane(
            root, settings_path=settings_path,
            extra_control_files=lane_control_files)
        authority_before = integrity.capture_authority_surface(root, target)
    except integrity.IntegrityError as exc:
        raise audit.AuditError(f"control_plane_preflight_failed:{exc}") from exc
    command = [str(claude), *arguments, "--settings", str(settings_path)]
    if enforce_os_write_guard:
        command = _guarded_child_command(
            command, root=root, settings_path=settings_path,
            extra_control_files=lane_os_guard_files,
            extra_control_trees=lane_control_trees)
    context = audit.start_invocation(
        audit_root=audit_root, target=target, workflow=workflow,
        run_id=run_id, invocation_id=invocation_id, now=now)
    env = dict(os.environ)
    for credential_name in secrets_loader.ALLOWLIST:
        env.pop(credential_name, None)
    env[secrets_loader.MODEL_ACCESS_DENY_ENV] = "1"
    env[AUDITED_CHILD_ENV] = "1"
    env.update({
        audit.ENV_KEYS["root"]: str(audit_root.resolve()),
        audit.ENV_KEYS["target"]: target,
        audit.ENV_KEYS["workflow"]: workflow,
        audit.ENV_KEYS["run_id"]: run_id,
        audit.ENV_KEYS["invocation_id"]: invocation_id,
        audit.ENV_KEYS["event_log"]: str(context["event_log"]),
    })
    if write_scope is not None:
        env[WRITE_ONLY_PATH_ENV] = str(write_scope)
    child_rc = 127
    child_process_group_id: int | None = None
    if _validated_outer_only_entrypoints() != trusted_entrypoint_identities:
        raise audit.AuditError("outer_only_entrypoint_identity_changed")
    # Open the writer fence only after every fallible launch prerequisite has passed. In
    # particular, a nested sandbox probe can fail before a child exists; opening the marker
    # before that probe stranded a live-looking 4,200-second window on 2026-07-28/29.
    # The control-plane snapshot is already complete, and the marker remains outside the
    # compared surface. TTL is only the abnormal-process-death backstop.
    marker_signal_mask = _block_termination_signals()
    try:
        window_marker = audited_window.open_window(
            root, lane=lane, run_id=run_id, invocation_id=invocation_id,
            now_utc=(now or dt.datetime.now(dt.timezone.utc)))
        remember_window_marker(window_marker)
    finally:
        # A signal delivered after the marker inode exists is held until its path is registered.
        # Restoring the mask then invokes _terminate_child, which unwinds before Popen.
        _restore_signal_mask(marker_signal_mask)
    if TERMINATION_SIGNAL is not None:
        raise TerminationRequested(TERMINATION_SIGNAL)
    try:
        launch_signal_mask = _block_termination_signals()
        try:
            if TERMINATION_SIGNAL is not None:
                raise TerminationRequested(TERMINATION_SIGNAL)
            ACTIVE_CHILD = subprocess.Popen(
                command, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                start_new_session=True)
            child_process_group_id = ACTIVE_CHILD.pid
        finally:
            # This is the linearization boundary: a signal either unwinds before launch or sees a
            # fully assigned ACTIVE_CHILD and kills its entire process group.
            _restore_signal_mask(launch_signal_mask)
        child_rc = ACTIVE_CHILD.wait()
    except OSError:
        child_rc = 127
    finally:
        if child_process_group_id is not None:
            _kill_remaining_child_process_group(child_process_group_id)
        ACTIVE_CHILD = None
    if child_rc < 0:
        child_rc = 128 + abs(child_rc)
    finished = audit._utc()
    receipt: dict | None = None
    sli: dict | None = None
    audit_finalization_error: str | None = None
    # Persist the broker-audit receipt FIRST, ahead of the long post-child work below
    # (control-plane comparison, authority comparison, quarantine). A SIGTERM/SIGINT delivered in
    # that window used to unwind the wrapper with no receipt at all, so a child that had already
    # exited cleanly still surfaced downstream as collector_audit_receipt_missing
    # (candidate-quotes lane, 2026-08-12). `finalize_invocation` reads only context/child_rc/
    # started/finished, and it writes exclusively under data/runtime/broker_audit/, which
    # control_plane_integrity excludes from BOTH compared surfaces (CONTROL_TREES/CONTROL_FILES
    # and _authority_paths), so ordering it before the comparisons cannot perturb the integrity
    # verdict. The existing critical-section primitive holds termination signals only for that
    # bounded write; any pending signal is delivered on mask restore and unwinds exactly as it did
    # before -- now with a durable receipt behind it. Nothing here is synthesized for a child
    # killed mid-flight: child_rc still carries 128+signo and every count still comes from the
    # real event log.
    try:
        finalize_signal_mask = _block_termination_signals()
        try:
            receipt = audit.finalize_invocation(
                context, child_exit_code=child_rc, started_utc=started,
                finished_utc=finished)
        finally:
            _restore_signal_mask(finalize_signal_mask)
        with context["sli_lock"].open("a+b") as lock:
            os.chmod(context["sli_lock"], 0o600)
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            sli = audit.summarize_target(audit_root=audit_root, target=target)
    except Exception as exc:
        # Audit finalization is part of the trust boundary. It must not bypass the same-child
        # authority cleanup merely because no valid broker receipt could be produced.
        audit_finalization_error = type(exc).__name__
    try:
        control_comparison = integrity.compare_control_plane(
            control_before, settings_path=settings_path)
    except Exception as exc:
        control_comparison = {
            "ok": False,
            "before_sha256": control_before.get(
                "comparison_sha256", control_before["snapshot_sha256"]),
            "after_sha256": None,
            "changed_paths": [f"comparison_error:{type(exc).__name__}"],
            "changed_count": 1,
            "after": {"projection": {}, "paths": {}, "contents": {}},
        }
    unauthorized_authority_paths: list[str] = []
    if lane == "morning-main":
        try:
            authority_comparison = integrity.compare_authority_surface(
                authority_before, root, target)
            unauthorized_authority_paths = [
                path for path in authority_comparison.get("changed_paths", [])
                if not _morning_authority_change_allowed(path, target)
            ]
        except Exception as exc:
            unauthorized_authority_paths = [
                f"authority_comparison_error:{type(exc).__name__}"]

    broker_violation = bool(
        isinstance(receipt, dict) and receipt.get("violation_detected") is True)

    restoration: dict | None = None
    authority_quarantine: dict | None = None
    quarantine_error: str | None = None
    # A failed child may have written a partial report or other authority-bearing state before
    # exiting. Never leave those bytes available to a retry or a later workflow: preserve them for
    # forensics, restore the exact pre-child surface, and return the original child status only
    # after quarantine itself has been verified.
    must_quarantine = (child_rc != 0
                       or control_comparison.get("ok") is not True
                       or bool(unauthorized_authority_paths)
                       or broker_violation
                       or audit_finalization_error is not None)
    if must_quarantine:
        try:
            quarantine = integrity.reserve_quarantine(root, target, invocation_id)
            if control_comparison.get("ok") is not True:
                restoration = integrity.restore_control_plane(
                    control_before, control_comparison, quarantine,
                    settings_path=settings_path)
            authority_quarantine = integrity.quarantine_authority_changes(
                authority_before, root, target, quarantine)
        except Exception as exc:
            quarantine_error = type(exc).__name__

    restoration_ok = (control_comparison.get("ok") is True
                      or (isinstance(restoration, dict)
                          and restoration.get("restored_exactly") is True
                          and not restoration.get("errors")))
    authority_quarantine_ok = (
        not must_quarantine
        or (isinstance(authority_quarantine, dict)
            and authority_quarantine.get("restored_exactly") is True
            and not authority_quarantine.get("errors")))
    integrity_receipt_path = (
        context["run_root"] / f"{invocation_id}.integrity.json")
    integrity_receipt: dict | None = None
    integrity_receipt_error: str | None = None
    try:
        integrity_receipt = integrity.write_receipt(
            integrity_receipt_path,
            target=target,
            workflow=workflow,
            run_id=run_id,
            invocation_id=invocation_id,
            comparison=control_comparison,
            restoration=restoration,
            authority=authority_quarantine,
            broker_violation=broker_violation,
            unauthorized_authority_paths=unauthorized_authority_paths,
            generated_utc=finished,
        )
    except Exception as exc:
        integrity_receipt_error = type(exc).__name__

    integrity_failure = (
        control_comparison.get("ok") is not True
        or not restoration_ok
        or not authority_quarantine_ok
        or bool(unauthorized_authority_paths)
        or quarantine_error is not None
        or integrity_receipt is None)
    if broker_violation:
        final_rc = 86
    elif integrity_failure:
        final_rc = CONTROL_PLANE_VIOLATION_EXIT
    elif audit_finalization_error is not None:
        final_rc = 87
    elif require_coverage and (receipt or {}).get("instrumentation_coverage") != "complete":
        final_rc = 87
    else:
        final_rc = child_rc
    result = {
        "ok": final_rc == 0,
        "child_exit_code": child_rc,
        "final_exit_code": final_rc,
        "invocation_id": invocation_id,
        "receipt_path": str(context["receipt"]),
        "receipt_sha256": (receipt or {}).get("receipt_sha256"),
        "instrumentation_coverage": (receipt or {}).get(
            "instrumentation_coverage", "unobservable"),
        "broker_read_attempt_total": (receipt or {}).get("broker_read_attempt_total"),
        "broker_preview_write_attempt_total": (receipt or {}).get(
            "broker_preview_write_attempt_total"),
        "unknown_broker_attempt_total": (receipt or {}).get("unknown_broker_attempt_total"),
        "violation_detected": ((receipt or {}).get("violation_detected")
                               if receipt is not None else None),
        "daily_sli_status": (sli or {}).get("status", "unobservable"),
        "write_scope_enforced": write_scope is not None,
        "control_plane_os_write_guard_enforced": bool(enforce_os_write_guard),
        "control_plane_os_write_guard_mode": guard_mode,
        "control_plane_integrity": (
            "verified_unchanged" if control_comparison.get("ok") is True
            else "violation_restored" if restoration_ok
            else "violation_restore_incomplete"),
        "control_plane_changed_count": control_comparison.get("changed_count", 0),
        "control_plane_restored_exactly": restoration_ok,
        "authority_outputs_quarantined": len(
            (authority_quarantine or {}).get("quarantined_authority_paths") or []),
        "unauthorized_authority_paths": unauthorized_authority_paths,
        "authority_quarantine_ok": authority_quarantine_ok,
        "integrity_receipt_path": (str(integrity_receipt_path)
                                   if integrity_receipt is not None else None),
        "integrity_receipt_sha256": ((integrity_receipt or {}).get("receipt_sha256")),
        "integrity_receipt_error": integrity_receipt_error,
        "audit_finalization_error": audit_finalization_error,
        "quarantine_error": quarantine_error,
        **audit.SAFETY,
    }
    return result, final_rc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claude CLI wrapper with broker runtime audit.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--audit-root")
    parser.add_argument("--target", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--settings", default=str(audit.SETTINGS_PATH))
    parser.add_argument("--require-coverage", action="store_true")
    parser.add_argument("--write-only-path")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    child_args = list(args.claude_args)
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]
    try:
        root = Path(args.root)
        result, rc = run_audited(
            root=root,
            audit_root=Path(args.audit_root) if args.audit_root else (
                root / "data" / "runtime" / "broker_audit"),
            target=args.target, workflow=args.workflow, run_id=args.run_id, lane=args.lane,
            claude=Path(args.claude), arguments=child_args,
            settings_path=Path(args.settings), require_coverage=args.require_coverage,
            write_only_path=Path(args.write_only_path) if args.write_only_path else None)
        print("broker_audit=" + json.dumps(result, sort_keys=True), file=sys.stderr)
        return rc
    except audit.AuditViolation as exc:
        print(f"audited_claude_violation:{exc}", file=sys.stderr)
        return 86
    except TerminationRequested as exc:
        print(f"audited_claude_terminated:signal={exc.signum}", file=sys.stderr)
        return exc.exit_code
    except (audit.AuditError, OSError, ValueError) as exc:
        print(f"audited_claude_error:{exc}", file=sys.stderr)
        return 87


if __name__ == "__main__":
    raise SystemExit(main())
