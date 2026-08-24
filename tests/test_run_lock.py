#!/usr/bin/env python3
"""Adversarial tests for atomic repository run-lock ownership."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import run_lock  # noqa: E402


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _release(self, result: dict) -> None:
        released = run_lock.release(
            self.root, pid=os.getpid(), nonce=str(result["nonce"]))
        self.assertTrue(released["ok"], released)

    def test_lock_appears_complete_and_release_requires_pid_plus_nonce(self) -> None:
        acquired = run_lock.acquire(self.root, pid=os.getpid())
        self.assertTrue(acquired["ok"], acquired)
        lock = self.root / run_lock.LOCK_NAME
        self.assertEqual({path.name for path in lock.iterdir()},
                         run_lock.METADATA_NAMES)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o700)
        for name in run_lock.METADATA_NAMES:
            info = (lock / name).lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_nlink, 1)
        self.assertEqual(run_lock.inspect(self.root)["status"], "valid_live")

        wrong = run_lock.release(
            self.root, pid=os.getpid(), nonce="0" * 64)
        self.assertFalse(wrong["ok"])
        self.assertTrue(lock.is_dir())
        self._release(acquired)
        self.assertEqual(run_lock.inspect(self.root)["status"], "absent")

    def test_strict_dead_owner_is_atomically_reclaimed(self) -> None:
        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        try:
            first = run_lock.acquire(self.root, pid=holder.pid)
            self.assertTrue(first["ok"], first)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        self.assertEqual(run_lock.inspect(self.root)["status"], "valid_dead")

        second = run_lock.acquire(self.root, pid=os.getpid())
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["reclaimed"])
        self.assertEqual(run_lock.inspect(self.root)["status"], "valid_live")
        self._release(second)

    def test_live_malformed_symlink_and_ambiguous_locks_are_never_reclaimed(self) -> None:
        live = run_lock.acquire(self.root, pid=os.getpid())
        contender = run_lock.acquire(self.root, pid=os.getpid())
        self.assertFalse(contender["ok"])
        self.assertEqual(contender["status"], "valid_live")
        self._release(live)

        malformed = self.root / run_lock.LOCK_NAME
        malformed.mkdir(mode=0o700)
        blocked = run_lock.acquire(self.root, pid=os.getpid())
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "malformed")
        self.assertTrue(malformed.is_dir())
        malformed.rmdir()

        target = self.root / "elsewhere"
        target.mkdir()
        malformed.symlink_to(target, target_is_directory=True)
        blocked = run_lock.acquire(self.root, pid=os.getpid())
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "symlink")
        self.assertTrue(malformed.is_symlink())
        malformed.unlink()

        current = run_lock.acquire(self.root, pid=os.getpid())
        with mock.patch.object(run_lock.os, "kill", side_effect=PermissionError):
            inspection = run_lock.inspect(self.root)
        self.assertEqual(inspection["status"], "ambiguous")
        self._release(current)

    def test_metadata_fault_never_publishes_partial_lock(self) -> None:
        with mock.patch.object(
                run_lock, "_write_metadata", side_effect=OSError("fault")):
            with self.assertRaises(OSError):
                run_lock.acquire(self.root, pid=os.getpid())
        self.assertEqual(run_lock.inspect(self.root)["status"], "absent")
        self.assertEqual(list(self.root.glob(".run.lock.acquire-*")), [])

    def test_only_one_of_many_concurrent_acquirers_wins(self) -> None:
        command = [
            sys.executable, str(ROOT / "scripts" / "lib" / "run_lock.py"),
            "--root", str(self.root), "acquire", "--pid", str(os.getpid()),
        ]
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
            for _ in range(20)
        ]
        completed = [process.communicate(timeout=20) + (process.returncode,)
                     for process in processes]
        winners = [row for row in completed if row[2] == 0]
        blocked = [row for row in completed if row[2] == run_lock.RC_CONTENDED]
        self.assertEqual(len(winners), 1, completed)
        self.assertEqual(len(blocked), 19, completed)
        nonce = winners[0][0].strip()
        self.assertRegex(nonce, r"^[0-9a-f]{64}$")
        self.assertEqual(run_lock.inspect(self.root)["status"], "valid_live")
        released = run_lock.release(self.root, pid=os.getpid(), nonce=nonce)
        self.assertTrue(released["ok"], released)

    def test_recovery_only_removes_a_strict_provably_dead_lock(self) -> None:
        clear = run_lock.recover_dead(self.root)
        self.assertTrue(clear["ok"])
        self.assertFalse(clear["recovered"])

        malformed = self.root / run_lock.LOCK_NAME
        malformed.mkdir(mode=0o700)
        refused = run_lock.recover_dead(self.root)
        self.assertFalse(refused["ok"])
        self.assertTrue(malformed.exists())
        malformed.rmdir()

        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        try:
            acquired = run_lock.acquire(self.root, pid=holder.pid)
            self.assertTrue(acquired["ok"], acquired)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        recovered = run_lock.recover_dead(self.root)
        self.assertTrue(recovered["ok"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual(run_lock.inspect(self.root)["status"], "absent")

    def test_cli_does_not_disclose_nonce_in_inspection(self) -> None:
        acquired = run_lock.acquire(self.root, pid=os.getpid())
        command = [
            sys.executable, str(ROOT / "scripts" / "lib" / "run_lock.py"),
            "--root", str(self.root), "inspect",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        public = json.loads(completed.stdout)
        self.assertNotIn("nonce", public)
        self.assertEqual(public["status"], "valid_live")
        self._release(acquired)

    def test_bash32_wrapper_acquires_and_releases_with_ownership_token(self) -> None:
        shell_root = self.root / "shell-root"
        library = shell_root / "scripts" / "lib"
        library.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "lib" / "common.sh", library / "common.sh")
        shutil.copy2(ROOT / "scripts" / "lib" / "run_lock.py", library / "run_lock.py")
        script = (
            "source scripts/lib/common.sh\n"
            "notify() { :; }\n"
            "acquire_lock\n"
            "python3 scripts/lib/run_lock.py --root \"$PROJECT_ROOT\" inspect\n"
            "release_lock\n"
            "test ! -e \"$LOCK_DIR\"\n"
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", script], cwd=shell_root, capture_output=True,
            text=True, check=False, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        inspected = json.loads(completed.stdout.splitlines()[0])
        self.assertEqual(inspected["status"], "valid_live")
        self.assertNotIn("nonce", inspected)

    def test_post_close_keeps_one_fail_closed_lock_attempt(self) -> None:
        text = (ROOT / "scripts" / "run_post_close_review.sh").read_text(
            encoding="utf-8")
        self.assertEqual(text.count("\nacquire_lock\n"), 1)
        self.assertNotIn("retry_acquire_lock", text)


# ---------------------------------------------------------------------------
# Regression cover for the 2026-07-29..07-31 outage: one abandoned legacy shell
# lock made every scheduled workflow exit 75 for three consecutive sessions.
# ---------------------------------------------------------------------------
class LegacyLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _legacy(self, *, pid: int, mode: int = 0o755, age_seconds: int = 0,
                pid_mode: int = 0o644) -> Path:
        """Reproduce exactly what the pre-2026-07-29 shell acquire_lock left behind."""
        lock = self.root / run_lock.LOCK_NAME
        os.mkdir(lock, mode)
        os.chmod(lock, mode)
        pid_file = lock / "pid"
        pid_file.write_text("%d\n" % pid, encoding="ascii")
        os.chmod(pid_file, pid_mode)
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(lock, (stamp, stamp))
        return lock

    def _dead_pid(self) -> int:
        holder = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        holder.wait(timeout=10)
        return holder.pid

    def test_aged_dead_legacy_lock_is_reclaimed_and_preserved(self) -> None:
        lock = self._legacy(pid=self._dead_pid(),
                            age_seconds=run_lock.LEGACY_MIN_AGE_SECONDS + 60)
        self.assertEqual(run_lock.inspect(self.root)["status"], "legacy_dead")

        acquired = run_lock.acquire(self.root, pid=os.getpid())
        self.assertTrue(acquired["ok"], acquired)
        self.assertTrue(acquired["reclaimed_legacy"])
        self.assertEqual(acquired["reason"], "legacy_dead_owner_reclaimed")
        self.assertEqual(run_lock.inspect(self.root)["status"], "valid_live")

        # The displaced lock is retained, never deleted: it is the only evidence of
        # why the sessions were missed.
        evidence = list(self.root.glob(".run.lock.quarantine-*"))
        self.assertEqual(len(evidence), 1, evidence)
        self.assertEqual(evidence[0].name, acquired["evidence"])
        self.assertTrue((evidence[0] / "pid").is_file())
        self.assertRegex(evidence[0].name, run_lock.QUARANTINE_RE)
        self.assertTrue(lock.is_dir())

        released = run_lock.release(
            self.root, pid=os.getpid(), nonce=str(acquired["nonce"]))
        self.assertTrue(released["ok"], released)

    def test_live_legacy_holder_is_never_reclaimed(self) -> None:
        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        try:
            self._legacy(pid=holder.pid,
                         age_seconds=run_lock.LEGACY_MIN_AGE_SECONDS + 60)
            inspected = run_lock.inspect(self.root)
            self.assertEqual(inspected["status"], "malformed")
            self.assertEqual(inspected["reason"], "legacy_lock_holder_running")
            blocked = run_lock.acquire(self.root, pid=os.getpid())
            self.assertFalse(blocked["ok"])
            self.assertTrue((self.root / run_lock.LOCK_NAME).is_dir())
            self.assertEqual(list(self.root.glob(".run.lock.quarantine-*")), [])
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    def test_recent_dead_legacy_lock_still_fails_closed(self) -> None:
        self._legacy(pid=self._dead_pid(), age_seconds=60)
        inspected = run_lock.inspect(self.root)
        self.assertEqual(inspected["status"], "malformed")
        self.assertEqual(inspected["reason"], "legacy_lock_too_recent")
        blocked = run_lock.acquire(self.root, pid=os.getpid())
        self.assertFalse(blocked["ok"])
        self.assertTrue((self.root / run_lock.LOCK_NAME).is_dir())

    def test_group_or_world_writable_legacy_pid_is_refused(self) -> None:
        self._legacy(pid=self._dead_pid(),
                     age_seconds=run_lock.LEGACY_MIN_AGE_SECONDS + 60,
                     pid_mode=0o666)
        inspected = run_lock.inspect(self.root)
        self.assertEqual(inspected["status"], "malformed")
        self.assertEqual(inspected["reason"], "legacy_pid_permissions_invalid")
        self.assertFalse(run_lock.acquire(self.root, pid=os.getpid())["ok"])

    def test_legacy_shape_with_unexpected_mode_stays_malformed(self) -> None:
        self._legacy(pid=self._dead_pid(), mode=0o777,
                     age_seconds=run_lock.LEGACY_MIN_AGE_SECONDS + 60)
        inspected = run_lock.inspect(self.root)
        self.assertEqual(inspected["status"], "malformed")
        self.assertEqual(inspected["reason"], "lock_permissions_invalid")

    def test_legacy_pid_garbage_is_refused(self) -> None:
        lock = self.root / run_lock.LOCK_NAME
        os.mkdir(lock, 0o755)
        os.chmod(lock, 0o755)
        (lock / "pid").write_text("not-a-pid\n", encoding="ascii")
        stamp = time.time() - run_lock.LEGACY_MIN_AGE_SECONDS - 60
        os.utime(lock, (stamp, stamp))
        inspected = run_lock.inspect(self.root)
        self.assertEqual(inspected["status"], "malformed")
        self.assertEqual(inspected["reason"], "legacy_pid_invalid")

    def test_recover_dead_clears_an_aged_legacy_lock(self) -> None:
        self._legacy(pid=self._dead_pid(),
                     age_seconds=run_lock.LEGACY_MIN_AGE_SECONDS + 60)
        recovered = run_lock.recover_dead(self.root)
        self.assertTrue(recovered["ok"], recovered)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(run_lock.inspect(self.root)["status"], "absent")
        self.assertEqual(len(list(self.root.glob(".run.lock.quarantine-*"))), 1)


class OperatorQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quarantine_requires_explicit_confirmation(self) -> None:
        (self.root / run_lock.LOCK_NAME).mkdir(mode=0o700)
        refused = run_lock.quarantine_unsafe(self.root, confirm=False)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "confirmation_required")
        self.assertTrue((self.root / run_lock.LOCK_NAME).is_dir())

    def test_quarantine_refuses_a_malformed_lock(self) -> None:
        """`malformed` is the residual "not recognised" bucket, so it is never quarantinable."""
        (self.root / run_lock.LOCK_NAME).mkdir(mode=0o700)
        refused = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertFalse(refused["ok"], refused)
        self.assertFalse(refused["quarantined"])
        self.assertEqual(refused["status"], "malformed")
        self.assertEqual(refused["reason"], "lock_metadata_shape_invalid")
        self.assertEqual(run_lock.inspect(self.root)["status"], "malformed")
        self.assertEqual(list(self.root.glob(".run.lock.quarantine-*")), [],
                         "a refused lock must never be moved aside")

    def test_quarantine_refuses_a_symlink_lock(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / run_lock.LOCK_NAME).symlink_to(target, target_is_directory=True)
        refused = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertFalse(refused["ok"], refused)
        self.assertFalse(refused["quarantined"])
        self.assertEqual(refused["status"], "symlink")
        self.assertTrue((self.root / run_lock.LOCK_NAME).is_symlink())
        self.assertTrue(target.is_dir())

    def test_quarantine_accepts_only_an_exact_legacy_dead_lock(self) -> None:
        holder = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        holder.wait(timeout=10)
        lock = self.root / run_lock.LOCK_NAME
        os.mkdir(lock, 0o755)
        (lock / "pid").write_text("%d\n" % holder.pid, encoding="ascii")
        os.chmod(lock / "pid", 0o644)
        stamp = time.time() - run_lock.LEGACY_MIN_AGE_SECONDS - 3600
        os.utime(lock, (stamp, stamp))
        self.assertEqual(run_lock.inspect(self.root)["status"], "legacy_dead")

        moved = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertTrue(moved["ok"], moved)
        self.assertTrue(moved["quarantined"])
        self.assertEqual(run_lock.inspect(self.root)["status"], "absent")
        evidence = list(self.root.glob(".run.lock.quarantine-*"))
        self.assertEqual([path.name for path in evidence], [moved["evidence"]])
        self.assertTrue((evidence[0] / "pid").is_file(), "evidence is preserved, not deleted")

    def test_quarantine_refuses_live_and_ambiguous_locks(self) -> None:
        acquired = run_lock.acquire(self.root, pid=os.getpid())
        refused = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "valid_live")
        self.assertFalse(refused["quarantined"])

        with mock.patch.object(run_lock.os, "kill", side_effect=PermissionError):
            ambiguous = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertTrue((self.root / run_lock.LOCK_NAME).is_dir())
        run_lock.release(self.root, pid=os.getpid(), nonce=str(acquired["nonce"]))

    def test_quarantine_on_clear_repository_is_a_no_op(self) -> None:
        clear = run_lock.quarantine_unsafe(self.root, confirm=True)
        self.assertTrue(clear["ok"])
        self.assertFalse(clear["quarantined"])
        self.assertEqual(clear["reason"], "lock_absent")

    def test_cli_quarantine_never_leaks_a_nonce_and_needs_confirm(self) -> None:
        # Exercised against legacy_dead, now the only status the command accepts.
        holder = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        holder.wait(timeout=10)
        lock = self.root / run_lock.LOCK_NAME
        os.mkdir(lock, 0o755)
        (lock / "pid").write_text("%d\n" % holder.pid, encoding="ascii")
        os.chmod(lock / "pid", 0o644)
        stamp = time.time() - run_lock.LEGACY_MIN_AGE_SECONDS - 3600
        os.utime(lock, (stamp, stamp))
        base = [sys.executable, str(ROOT / "scripts" / "lib" / "run_lock.py"),
                "--root", str(self.root), "quarantine"]
        unconfirmed = subprocess.run(base, capture_output=True, text=True,
                                     check=False, timeout=10)
        self.assertEqual(unconfirmed.returncode, run_lock.RC_CONTENDED)
        self.assertTrue((self.root / run_lock.LOCK_NAME).is_dir())

        confirmed = subprocess.run(base + ["--confirm"], capture_output=True,
                                   text=True, check=False, timeout=10)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        payload = json.loads(confirmed.stdout)
        self.assertNotIn("nonce", payload)
        self.assertTrue(payload["quarantined"])


class ContentionDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_contention_reports_status_pid_and_reason(self) -> None:
        (self.root / run_lock.LOCK_NAME).mkdir(mode=0o700)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lib" / "run_lock.py"), "--root",
             str(self.root), "acquire", "--pid", str(os.getpid())],
            capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(completed.returncode, run_lock.RC_CONTENDED)
        fields = completed.stdout.strip().split(":")
        self.assertEqual(len(fields), 3, completed.stdout)
        self.assertEqual(fields[0], "malformed")
        self.assertEqual(fields[1], "?")
        self.assertEqual(fields[2], "lock_metadata_shape_invalid")

    def test_shell_wrapper_surfaces_the_reason_in_its_refusal(self) -> None:
        shell_root = self.root / "shell-root"
        library = shell_root / "scripts" / "lib"
        library.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "lib" / "common.sh", library / "common.sh")
        shutil.copy2(ROOT / "scripts" / "lib" / "run_lock.py", library / "run_lock.py")
        (shell_root / run_lock.LOCK_NAME).mkdir(mode=0o700)
        script = (
            "source scripts/lib/common.sh\n"
            "notify() { :; }\n"
            "acquire_lock\n"
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", script], cwd=shell_root, capture_output=True,
            text=True, check=False, timeout=20)
        self.assertEqual(completed.returncode, 75, completed.stderr)
        errors = (shell_root / "logs" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("status=malformed", errors)
        self.assertIn("reason=lock_metadata_shape_invalid", errors)


if __name__ == "__main__":
    unittest.main()
