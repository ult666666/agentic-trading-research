#!/usr/bin/env python3
"""Versioned launchd contract — the HERMETIC half of scheduler verification.

WHY THIS EXISTS (2026-07-28 integration failure)
------------------------------------------------
`launchd/*.plist` are INSTALL ARTIFACTS. launchd requires absolute paths, so every tracked plist
necessarily bakes the canonical production install root
(`/Users/prince/Projects/trading-system`) into `ProgramArguments` and `WorkingDirectory`. That is
correct and must not change.

`scripts/test_macro_actual_refresh.py` asserted
``plist["ProgramArguments"] == ["/bin/bash", str(ROOT / "scripts" / "run_macro_actual_refresh.sh")]``
where ``ROOT`` is *the worktree currently running the test*. That assertion therefore holds ONLY
when the checkout happens to be the production path. It passed in production and in the
production-path fixture, and failed — correctly and unavoidably — inside the clean integration
candidate at `/Users/prince/Projects/.trading-worktrees/p0-integration-candidate`, which is what
blocked the integration.

The two questions were conflated. They are now separated:

  VERSIONED CODE CONTRACT (this module, hermetic, passes in ANY worktree)
      Do the tracked plists declare a single consistent install root, the right labels,
      schedules, environment and runner tails, and does each referenced runner exist in THIS
      tree? No OS state is consulted.

  INSTALLED MAC STATE (scripts/launchd_scheduler_health.py, operational audit)
      Does the machine's installed LaunchAgent match the tracked plist, is the install root the
      real production path, is exactly one job registered, is the wake schedule right? That
      module already compares against ``ROOT`` and is only ever run from production, where
      ``ROOT`` IS the install root — which is correct there and is left untouched.

Read-only, std-lib only. Consults no launchctl/pmset state and mutates nothing.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

SCHEMA = "launchd_versioned_contract/1.0"

# The canonical production install root is a VERSIONED FACT declared by the tracked plists
# themselves. It is deliberately NOT derived from the running worktree.
CANONICAL_INSTALL_ROOT = "/Users/prince/Projects/trading-system"

EXPECTED_RUNNERS = {
    "com.charles.trading.intake": "scripts/run_data_intake.sh",
    "com.charles.trading.macro-refresh": "scripts/run_macro_actual_refresh.sh",
    "com.charles.trading.morning": "scripts/run_morning_scan.sh",
    "com.charles.trading.premarket": "scripts/run_premarket_refresh.sh",
    "com.charles.trading.postclose": "scripts/run_post_close_review.sh",
    "com.charles.trading.watchdog": "scripts/run_autonomy_watchdog.sh",
}
EXPECTED_SCHEDULES = {
    "com.charles.trading.intake": {"Hour": 5, "Minute": 0},
    "com.charles.trading.macro-refresh": {"Hour": 5, "Minute": 35},
    "com.charles.trading.morning": {"Hour": 5, "Minute": 38},
    "com.charles.trading.premarket": {"Hour": 6, "Minute": 45},
    "com.charles.trading.postclose": {"Hour": 13, "Minute": 15},
}
ABSOLUTE_RE = re.compile(r"^/(?:[^/\0]+/)*[^/\0]+$")


class ContractError(ValueError):
    pass


def split_program_argument(argument: str) -> tuple[str, str]:
    """Split an absolute ``ProgramArguments[1]`` into (install_root, repo_relative_path).

    The split is anchored on the LAST ``/scripts/`` segment so an install root that itself
    contains the word ``scripts`` cannot shift the boundary.
    """
    if not isinstance(argument, str) or not ABSOLUTE_RE.fullmatch(argument):
        raise ContractError("program_argument_not_absolute")
    marker = "/scripts/"
    index = argument.rfind(marker)
    if index <= 0:
        raise ContractError("program_argument_missing_scripts_segment")
    return argument[:index], argument[index + 1:]


def load_repo_plist(root: Path, label: str) -> dict:
    path = Path(root) / "launchd" / f"{label}.plist"
    if not path.is_file():
        raise ContractError(f"repo_plist_missing:{label}")
    with path.open("rb") as handle:
        return plistlib.load(handle)


def declared_install_root(document: dict) -> str:
    argv = document.get("ProgramArguments")
    if not isinstance(argv, list) or len(argv) != 2 or argv[0] != "/bin/bash":
        raise ContractError("program_arguments_shape_invalid")
    install_root, _relative = split_program_argument(argv[1])
    return install_root


def verify_repo_plist(root: Path, label: str, *,
                      install_root: str = CANONICAL_INSTALL_ROOT) -> list[str]:
    """Verify ONE tracked plist against the versioned contract. Returns error tokens.

    Hermetic: the only filesystem access inside ``root`` is existence/executability of the
    referenced runner. No installed-state, launchctl, or pmset access.
    """
    errors: list[str] = []
    try:
        document = load_repo_plist(root, label)
    except ContractError as exc:
        return [str(exc)]

    if document.get("Label") != label:
        errors.append("label_mismatch")

    argv = document.get("ProgramArguments")
    if not isinstance(argv, list) or len(argv) != 2 or argv[0] != "/bin/bash":
        errors.append("program_arguments_shape_invalid")
    else:
        try:
            declared_root, relative = split_program_argument(argv[1])
        except ContractError as exc:
            errors.append(str(exc))
        else:
            if declared_root != install_root:
                errors.append("install_root_mismatch")
            expected_relative = EXPECTED_RUNNERS.get(label)
            if expected_relative is None:
                errors.append("label_not_allowlisted")
            elif relative != expected_relative:
                errors.append("runner_relative_path_mismatch")
            else:
                runner = Path(root) / relative
                if not runner.is_file():
                    errors.append("runner_missing_in_tree")

    working = document.get("WorkingDirectory")
    if working != install_root:
        errors.append("working_directory_mismatch")

    expected_schedule = EXPECTED_SCHEDULES.get(label)
    if expected_schedule is not None:
        if document.get("StartCalendarInterval") != expected_schedule:
            errors.append("schedule_mismatch")

    environment = document.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        errors.append("environment_missing")
    elif label in EXPECTED_SCHEDULES and \
            environment.get("TRADING_AUTONOMY_ENABLE") != "1":
        errors.append("autonomy_enable_missing")

    return sorted(set(errors))


def verify_all_repo_plists(root: Path, *,
                           install_root: str = CANONICAL_INSTALL_ROOT) -> dict:
    """Verify every allowlisted tracked plist and prove they share ONE install root."""
    per_label = {label: verify_repo_plist(root, label, install_root=install_root)
                 for label in sorted(EXPECTED_RUNNERS)}
    roots: set[str] = set()
    for label in sorted(EXPECTED_RUNNERS):
        try:
            roots.add(declared_install_root(load_repo_plist(root, label)))
        except ContractError:
            continue
    errors = sorted({error for rows in per_label.values() for error in rows})
    if len(roots) > 1:
        errors.append("install_roots_inconsistent")
    return {
        "schema": SCHEMA,
        "install_root": install_root,
        "declared_install_roots": sorted(roots),
        "labels_checked": sorted(EXPECTED_RUNNERS),
        "errors_by_label": per_label,
        "errors": sorted(set(errors)),
        "ok": not errors,
    }


def verify_installed_state(*, installed: dict, registered_labels: list[str],
                           wake_schedule: str,
                           install_root: str = CANONICAL_INSTALL_ROOT) -> list[str]:
    """Audit INSTALLED OS state from EXPLICITLY INJECTED fixture data.

    Never calls launchctl or pmset itself, so it is unit-testable and can never mutate the
    machine. The operator script supplies the real observations.

    ``installed`` maps label -> parsed plist dict as installed under ~/Library/LaunchAgents.
    """
    errors: list[str] = []
    for label in sorted(EXPECTED_RUNNERS):
        document = installed.get(label)
        if not isinstance(document, dict):
            errors.append(f"installed_plist_missing:{label}")
            continue
        argv = document.get("ProgramArguments")
        if not isinstance(argv, list) or len(argv) != 2 or argv[0] != "/bin/bash":
            errors.append(f"installed_program_arguments_invalid:{label}")
            continue
        try:
            declared_root, relative = split_program_argument(argv[1])
        except ContractError:
            errors.append(f"installed_program_arguments_invalid:{label}")
            continue
        if declared_root != install_root:
            errors.append(f"installed_install_root_mismatch:{label}")
        if relative != EXPECTED_RUNNERS[label]:
            errors.append(f"installed_runner_mismatch:{label}")
        if document.get("WorkingDirectory") != install_root:
            errors.append(f"installed_working_directory_mismatch:{label}")
        expected_schedule = EXPECTED_SCHEDULES.get(label)
        if expected_schedule is not None \
                and document.get("StartCalendarInterval") != expected_schedule:
            errors.append(f"installed_schedule_mismatch:{label}")
        if registered_labels.count(label) > 1:
            errors.append(f"installed_duplicate_job:{label}")
        if label not in registered_labels:
            errors.append(f"installed_job_not_registered:{label}")
    if "wakepoweron at 4:55AM weekdays only" not in str(wake_schedule):
        errors.append("wake_schedule_mismatch")
    return sorted(set(errors))
