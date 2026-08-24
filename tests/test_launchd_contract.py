#!/usr/bin/env python3
"""Regression matrix for the versioned launchd contract vs the installed-state audit.

These tests are HERMETIC: they run from any worktree path, consult no launchctl/pmset state,
and mutate nothing. Installed-OS cases are driven by explicitly injected fixture dictionaries.

This is the coverage that replaces the non-hermetic assertion in
`test_macro_actual_refresh.py::test_schedule_runner_and_supervisor_contract`, which compared a
tracked plist's baked-in absolute production path against whatever worktree happened to be
running the test and therefore could only ever pass in production itself.

Run: python3 -m pytest scripts/test_launchd_contract.py -q
"""
from __future__ import annotations

import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import launchd_contract as contract  # noqa: E402

LABEL = "com.charles.trading.macro-refresh"
PROD = contract.CANONICAL_INSTALL_ROOT


def stage_tree(destination: Path, *, labels=None) -> Path:
    """Copy the tracked launchd/ + referenced runners into an arbitrary root."""
    labels = labels or sorted(contract.EXPECTED_RUNNERS)
    (destination / "launchd").mkdir(parents=True, exist_ok=True)
    for label in labels:
        shutil.copy2(ROOT / "launchd" / f"{label}.plist",
                     destination / "launchd" / f"{label}.plist")
        relative = contract.EXPECTED_RUNNERS[label]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return destination


def write_plist(root: Path, label: str, document: dict) -> None:
    path = root / "launchd" / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(document, handle)


def read_plist(root: Path, label: str) -> dict:
    with (root / "launchd" / f"{label}.plist").open("rb") as handle:
        return plistlib.load(handle)


class TestProgramArgumentSplit(unittest.TestCase):
    def test_splits_on_last_scripts_segment(self):
        root, relative = contract.split_program_argument(
            "/Users/prince/Projects/trading-system/scripts/run_morning_scan.sh")
        self.assertEqual(root, "/Users/prince/Projects/trading-system")
        self.assertEqual(relative, "scripts/run_morning_scan.sh")

    def test_install_root_containing_scripts_word_is_not_confused(self):
        root, relative = contract.split_program_argument(
            "/Users/x/my-scripts-repo/scripts/run_morning_scan.sh")
        self.assertEqual(root, "/Users/x/my-scripts-repo")
        self.assertEqual(relative, "scripts/run_morning_scan.sh")

    def test_relative_or_malformed_rejected(self):
        for bad in ("scripts/run_morning_scan.sh", "", "run.sh", "/no_scripts_here.sh"):
            with self.assertRaises(contract.ContractError):
                contract.split_program_argument(bad)


class TestVersionedContractIsPathIndependent(unittest.TestCase):
    """The same tracked bytes must verify from ANY worktree path."""

    def test_passes_from_the_real_repository_checkout(self):
        self.assertEqual(contract.verify_repo_plist(ROOT, LABEL), [])

    def test_passes_from_a_linked_worktree_style_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = stage_tree(Path(td) / ".trading-worktrees" / "p0-integration-candidate")
            self.assertEqual(contract.verify_repo_plist(root, LABEL), [])

    def test_passes_from_an_arbitrary_temporary_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = stage_tree(Path(td) / "somewhere" / "else")
            self.assertEqual(contract.verify_repo_plist(root, LABEL), [])

    def test_all_tracked_plists_share_one_install_root(self):
        report = contract.verify_all_repo_plists(ROOT)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["declared_install_roots"], [PROD])
        self.assertEqual(report["errors"], [])


class TestVersionedContractDetectsRealDefects(unittest.TestCase):
    def _staged(self, td: str) -> Path:
        return stage_tree(Path(td) / "tree")

    def test_wrong_program_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, LABEL)
            doc["ProgramArguments"] = ["/bin/bash", f"{PROD}/scripts/run_morning_scan.sh"]
            write_plist(root, LABEL, doc)
            self.assertIn("runner_relative_path_mismatch",
                          contract.verify_repo_plist(root, LABEL))

    def test_wrong_install_root_in_program_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, LABEL)
            doc["ProgramArguments"] = ["/bin/bash",
                                       "/opt/elsewhere/scripts/run_macro_actual_refresh.sh"]
            write_plist(root, LABEL, doc)
            self.assertIn("install_root_mismatch", contract.verify_repo_plist(root, LABEL))

    def test_wrong_working_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, LABEL)
            doc["WorkingDirectory"] = "/opt/elsewhere"
            write_plist(root, LABEL, doc)
            self.assertIn("working_directory_mismatch", contract.verify_repo_plist(root, LABEL))

    def test_wrong_schedule(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, LABEL)
            doc["StartCalendarInterval"] = {"Hour": 9, "Minute": 0}
            write_plist(root, LABEL, doc)
            self.assertIn("schedule_mismatch", contract.verify_repo_plist(root, LABEL))

    def test_missing_autonomy_enable(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, LABEL)
            doc["EnvironmentVariables"] = {}
            write_plist(root, LABEL, doc)
            self.assertIn("autonomy_enable_missing", contract.verify_repo_plist(root, LABEL))

    def test_missing_runner_in_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            (root / contract.EXPECTED_RUNNERS[LABEL]).unlink()
            self.assertIn("runner_missing_in_tree", contract.verify_repo_plist(root, LABEL))

    def test_missing_plist(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            (root / "launchd" / f"{LABEL}.plist").unlink()
            self.assertEqual(contract.verify_repo_plist(root, LABEL),
                             [f"repo_plist_missing:{LABEL}"])

    def test_inconsistent_install_roots_across_plists(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._staged(td)
            doc = read_plist(root, "com.charles.trading.morning")
            doc["ProgramArguments"] = ["/bin/bash",
                                       "/opt/elsewhere/scripts/run_morning_scan.sh"]
            write_plist(root, "com.charles.trading.morning", doc)
            report = contract.verify_all_repo_plists(root)
            self.assertFalse(report["ok"])
            self.assertIn("install_roots_inconsistent", report["errors"])


class TestInstalledStateAuditFromFixtures(unittest.TestCase):
    """Installed-OS audit, driven ONLY by injected fixtures — never launchctl/pmset."""

    WAKE = "Repeating power events:\n  wakepoweron at 4:55AM weekdays only\n"

    def _installed(self) -> dict:
        return {label: {
            "Label": label,
            "ProgramArguments": ["/bin/bash", f"{PROD}/{relative}"],
            "WorkingDirectory": PROD,
            "StartCalendarInterval": contract.EXPECTED_SCHEDULES.get(label),
        } for label, relative in contract.EXPECTED_RUNNERS.items()}

    def test_healthy_installed_state(self):
        self.assertEqual(contract.verify_installed_state(
            installed=self._installed(),
            registered_labels=sorted(contract.EXPECTED_RUNNERS),
            wake_schedule=self.WAKE), [])

    def test_stale_installed_plist_points_at_old_path(self):
        installed = self._installed()
        installed[LABEL]["ProgramArguments"] = [
            "/bin/bash", "/Users/prince/Desktop/trading-system/scripts/run_macro_actual_refresh.sh"]
        errors = contract.verify_installed_state(
            installed=installed, registered_labels=sorted(contract.EXPECTED_RUNNERS),
            wake_schedule=self.WAKE)
        self.assertIn(f"installed_install_root_mismatch:{LABEL}", errors)

    def test_duplicate_installed_job(self):
        errors = contract.verify_installed_state(
            installed=self._installed(),
            registered_labels=sorted(contract.EXPECTED_RUNNERS) + [LABEL],
            wake_schedule=self.WAKE)
        self.assertIn(f"installed_duplicate_job:{LABEL}", errors)

    def test_job_not_registered(self):
        registered = [l for l in sorted(contract.EXPECTED_RUNNERS) if l != LABEL]
        errors = contract.verify_installed_state(
            installed=self._installed(), registered_labels=registered,
            wake_schedule=self.WAKE)
        self.assertIn(f"installed_job_not_registered:{LABEL}", errors)

    def test_missing_installed_plist(self):
        installed = self._installed(); installed.pop(LABEL)
        errors = contract.verify_installed_state(
            installed=installed, registered_labels=sorted(contract.EXPECTED_RUNNERS),
            wake_schedule=self.WAKE)
        self.assertIn(f"installed_plist_missing:{LABEL}", errors)

    def test_wrong_wake_schedule(self):
        errors = contract.verify_installed_state(
            installed=self._installed(), registered_labels=sorted(contract.EXPECTED_RUNNERS),
            wake_schedule="wakepoweron at 9:00AM weekdays only")
        self.assertIn("wake_schedule_mismatch", errors)


class TestTheTwoContractsAreIndependent(unittest.TestCase):
    """The 2026-07-28 lesson: these two must be able to disagree, and each must be detectable."""

    WAKE = "wakepoweron at 4:55AM weekdays only"

    def test_versioned_ok_while_installed_state_wrong(self):
        with tempfile.TemporaryDirectory() as td:
            root = stage_tree(Path(td) / "candidate")
            self.assertEqual(contract.verify_repo_plist(root, LABEL), [])
            installed = {label: {
                "Label": label,
                "ProgramArguments": ["/bin/bash", f"/opt/wrong/{relative}"],
                "WorkingDirectory": "/opt/wrong",
                "StartCalendarInterval": contract.EXPECTED_SCHEDULES.get(label),
            } for label, relative in contract.EXPECTED_RUNNERS.items()}
            errors = contract.verify_installed_state(
                installed=installed, registered_labels=sorted(contract.EXPECTED_RUNNERS),
                wake_schedule=self.WAKE)
            self.assertTrue(any(e.startswith("installed_install_root_mismatch") for e in errors))

    def test_installed_state_ok_while_versioned_contract_wrong(self):
        with tempfile.TemporaryDirectory() as td:
            root = stage_tree(Path(td) / "candidate")
            doc = read_plist(root, LABEL)
            doc["StartCalendarInterval"] = {"Hour": 4, "Minute": 4}
            write_plist(root, LABEL, doc)
            self.assertIn("schedule_mismatch", contract.verify_repo_plist(root, LABEL))
            installed = {label: {
                "Label": label,
                "ProgramArguments": ["/bin/bash", f"{PROD}/{relative}"],
                "WorkingDirectory": PROD,
                "StartCalendarInterval": contract.EXPECTED_SCHEDULES.get(label),
            } for label, relative in contract.EXPECTED_RUNNERS.items()}
            self.assertEqual(contract.verify_installed_state(
                installed=installed, registered_labels=sorted(contract.EXPECTED_RUNNERS),
                wake_schedule=self.WAKE), [])

    def test_no_launchctl_or_pmset_invocation_in_this_module(self):
        """The module may DESCRIBE the installed-state audit but must never perform it."""
        source = (SCRIPTS / "lib" / "launchd_contract.py").read_text()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("import subprocess", "subprocess.", "os.system", "os.popen",
                          "Popen(", "check_output(", 'run(["launchctl"', 'run(["pmset"'):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
