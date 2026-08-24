#!/usr/bin/env python3
"""Atomic, fail-closed ownership for the repository-wide workflow lock.

The visible ``.run.lock`` directory is never populated in place.  A complete private
directory is built first and atomically renamed into place.  Only a strictly valid lock
whose recorded process is provably absent may be reclaimed.  Live, malformed, symlinked,
and otherwise ambiguous locks are left untouched.

This module is operational coordination only.  It never invokes a broker, model, network
service, or trading action.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Iterator

LOCK_NAME = ".run.lock"
METADATA_NAMES = frozenset({"pid", "nonce", "created_utc"})
# The pre-2026-07-29 shell implementation created the lock with `mkdir` + a single `pid`
# file. That shape is not a valid modern lock, but it IS a precisely recognizable legacy
# artifact rather than an unknown one. Recognizing it is what prevents a repeat of the
# 2026-07-29..07-31 outage, where one abandoned legacy lock made every scheduled workflow
# exit 75 for three consecutive sessions with no reclaim path.
LEGACY_METADATA_NAMES = frozenset({"pid"})
LEGACY_MODES = frozenset({0o700, 0o755})
# Matches the historical shell rule: an abandoned lock older than 6h cannot belong to a
# live acquisition. Applied ON TOP OF a provably dead owner, never instead of it.
LEGACY_MIN_AGE_SECONDS = 21600
PID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
STAGE_RE = re.compile(r"^\.run\.lock\.(?:acquire|reclaim|release)-[0-9a-f]{64}$")
# Quarantined evidence is NEVER deleted, so it deliberately does not match STAGE_RE.
QUARANTINE_RE = re.compile(r"^\.run\.lock\.quarantine-[0-9a-f]{64}$")
MAX_METADATA_BYTES = 128
RC_CONTENDED = 75
RC_USAGE = 78
# The ONLY status an operator may quarantine by hand. `legacy_dead` is the one classification
# that has proven every condition required to move a lock aside safely: exact historical shell
# shape, this uid, an acceptable historical mode, a syntactically valid pid, a provably dead
# holder, and an age past the 6h floor.
#
# Everything else is excluded on purpose, including `malformed` and `symlink`. `malformed` is
# the residual "not recognised" bucket, so quarantining it would move an object nobody has
# diagnosed. Those states stay blocked pending forensic diagnosis; the cost is that they have no
# one-command clear, which is the intended trade.
QUARANTINABLE = frozenset({"legacy_dead"})
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004


class RunLockError(RuntimeError):
    """The lock operation could not be completed safely."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _created_text(now: dt.datetime | None = None) -> str:
    value = (now or _utc_now()).astimezone(dt.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _root_path(root: Path | str) -> Path:
    path = Path(root)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RunLockError("root_unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RunLockError("root_unsafe")
    return path


@contextlib.contextmanager
def _serialized() -> Iterator[None]:
    """Serialize helper operations without introducing another stale lock.

    Every legitimate lock operation runs through this immutable helper, so an advisory
    lock on this source inode closes acquire/reclaim/release ABA races.  Kernel release on
    process exit means this control lock cannot itself become stale.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(Path(__file__), flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_metadata(path: Path, name: str, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path / name, flags, 0o600)
    try:
        payload = (value + "\n").encode("ascii")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise RunLockError("metadata_write_incomplete")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_remove_private(path: Path) -> None:
    """Remove only a helper-created, strictly named private directory."""
    if not STAGE_RE.fullmatch(path.name):
        raise RunLockError("private_path_name_invalid")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RunLockError("private_path_unsafe")
    for child in path.iterdir():
        child_info = child.lstat()
        if child.is_symlink() or not stat.S_ISREG(child_info.st_mode):
            raise RunLockError("private_child_unsafe")
        child.unlink()
    path.rmdir()


def _make_stage(root: Path, *, pid: int, nonce: str,
                now: dt.datetime | None = None) -> Path:
    stage = root / f"{LOCK_NAME}.acquire-{nonce}"
    os.mkdir(stage, 0o700)
    try:
        _write_metadata(stage, "pid", str(pid))
        _write_metadata(stage, "nonce", nonce)
        _write_metadata(stage, "created_utc", _created_text(now))
        _fsync_directory(stage)
        if {entry.name for entry in stage.iterdir()} != METADATA_NAMES:
            raise RunLockError("stage_metadata_shape_invalid")
        return stage
    except Exception:
        with contextlib.suppress(Exception):
            _safe_remove_private(stage)
        raise


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a directory without ever replacing the destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_exclusive = getattr(libc, "renamex_np", None)
        if rename_exclusive is None:
            raise RunLockError("atomic_noreplace_unavailable")
        result = rename_exclusive(
            ctypes.c_char_p(source_bytes), ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(_RENAME_EXCL))
    elif sys.platform.startswith("linux"):
        rename_at2 = getattr(libc, "renameat2", None)
        if rename_at2 is None:
            raise RunLockError("atomic_noreplace_unavailable")
        result = rename_at2(
            ctypes.c_int(_AT_FDCWD), ctypes.c_char_p(source_bytes),
            ctypes.c_int(_AT_FDCWD), ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(_RENAME_NOREPLACE))
    else:
        raise RunLockError("atomic_noreplace_unavailable")
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(code, os.strerror(code), str(destination))
        raise OSError(code, os.strerror(code), str(destination))


def _read_metadata_file(path: Path) -> tuple[str | None, str | None]:
    try:
        info = path.lstat()
    except OSError:
        return None, "metadata_unreadable"
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        return None, "metadata_not_regular"
    if info.st_nlink != 1:
        return None, "metadata_link_count_invalid"
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        return None, "metadata_permissions_invalid"
    if info.st_size <= 0 or info.st_size > MAX_METADATA_BYTES:
        return None, "metadata_size_invalid"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_METADATA_BYTES + 1)
    except OSError:
        return None, "metadata_unreadable"
    if len(raw) > MAX_METADATA_BYTES:
        return None, "metadata_size_invalid"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, "metadata_encoding_invalid"
    if not text.endswith("\n") or "\n" in text[:-1] or "\r" in text:
        return None, "metadata_format_invalid"
    return text[:-1], None


def _read_legacy_pid(path: Path) -> tuple[int | None, str | None]:
    """Read a pre-2026-07-29 shell `pid` file without accepting an unsafe file.

    The shell wrote this file under the ambient umask, so 0644 is normal and 0600 is not
    required.  Any group/other WRITE bit is still refused: a lock another account can edit
    can never be treated as evidence about who owns this repository.
    """
    try:
        info = path.lstat()
    except OSError:
        return None, "legacy_pid_unreadable"
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        return None, "legacy_pid_not_regular"
    if info.st_nlink != 1:
        return None, "legacy_pid_link_count_invalid"
    if info.st_uid != os.getuid() or (stat.S_IMODE(info.st_mode) & 0o022):
        return None, "legacy_pid_permissions_invalid"
    if info.st_size <= 0 or info.st_size > MAX_METADATA_BYTES:
        return None, "legacy_pid_size_invalid"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_METADATA_BYTES + 1)
    except OSError:
        return None, "legacy_pid_unreadable"
    if len(raw) > MAX_METADATA_BYTES:
        return None, "legacy_pid_size_invalid"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, "legacy_pid_encoding_invalid"
    if not PID_RE.fullmatch(text.strip()):
        return None, "legacy_pid_invalid"
    return int(text.strip()), None


def _classify_legacy(lock: Path, lock_info: os.stat_result, *,
                     now: dt.datetime | None = None) -> dict:
    """Classify a recognizable legacy shell lock.

    Only `legacy_dead` is reclaimable, and only when the recorded owner is provably gone
    AND the directory is older than the historical 6h abandonment rule.  Every other legacy
    outcome keeps its blocking status so the fail-closed posture is unchanged; the value
    added here is a precise, actionable reason instead of a bare `malformed`.
    """
    pid, error = _read_legacy_pid(lock / "pid")
    if error is not None or pid is None:
        return {"status": "malformed", "reason": error or "legacy_pid_invalid"}
    status, _ = _process_status(pid)
    if status == "valid_live":
        return {"status": "malformed", "reason": "legacy_lock_holder_running",
                "pid": pid}
    if status == "ambiguous":
        return {"status": "ambiguous", "reason": "legacy_holder_liveness_unknown",
                "pid": pid}
    current = (now or _utc_now()).astimezone(dt.timezone.utc)
    age = current.timestamp() - lock_info.st_mtime
    if age < LEGACY_MIN_AGE_SECONDS:
        return {"status": "malformed", "reason": "legacy_lock_too_recent", "pid": pid}
    try:
        current_info = lock.lstat()
    except OSError:
        return {"status": "ambiguous", "reason": "lock_changed_during_inspection"}
    if (current_info.st_dev, current_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
        return {"status": "ambiguous", "reason": "lock_changed_during_inspection"}
    return {"status": "legacy_dead", "reason": "legacy_dead_owner", "pid": pid}


def _process_status(pid: int) -> tuple[str, str]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "valid_dead", "holder_not_running"
    except PermissionError:
        return "ambiguous", "holder_permission_denied"
    except OSError:
        return "ambiguous", "holder_liveness_unknown"
    return "valid_live", "holder_running"


def _inspect_unlocked(root: Path, *, now: dt.datetime | None = None,
                      include_private: bool = False) -> dict:
    lock = root / LOCK_NAME
    try:
        lock_info = lock.lstat()
    except FileNotFoundError:
        return {"status": "absent", "reason": "lock_absent"}
    except OSError:
        return {"status": "ambiguous", "reason": "lock_unreadable"}
    if lock.is_symlink():
        return {"status": "symlink", "reason": "lock_symlink"}
    if not stat.S_ISDIR(lock_info.st_mode):
        return {"status": "malformed", "reason": "lock_not_directory"}
    if lock_info.st_uid != os.getuid():
        return {"status": "malformed", "reason": "lock_permissions_invalid"}
    mode = stat.S_IMODE(lock_info.st_mode)
    try:
        entries = {entry.name for entry in lock.iterdir()}
    except OSError:
        return {"status": "ambiguous", "reason": "lock_entries_unreadable"}
    if entries == LEGACY_METADATA_NAMES and mode in LEGACY_MODES:
        return _classify_legacy(lock, lock_info, now=now)
    if mode != 0o700:
        return {"status": "malformed", "reason": "lock_permissions_invalid"}
    if entries != METADATA_NAMES:
        return {"status": "malformed", "reason": "lock_metadata_shape_invalid"}
    values: dict[str, str] = {}
    for name in sorted(METADATA_NAMES):
        value, error = _read_metadata_file(lock / name)
        if error:
            return {"status": "malformed", "reason": error}
        assert value is not None
        values[name] = value
    if not PID_RE.fullmatch(values["pid"]):
        return {"status": "malformed", "reason": "pid_invalid"}
    if not NONCE_RE.fullmatch(values["nonce"]):
        return {"status": "malformed", "reason": "nonce_invalid"}
    try:
        created = dt.datetime.fromisoformat(values["created_utc"].replace("Z", "+00:00"))
    except ValueError:
        return {"status": "malformed", "reason": "created_utc_invalid"}
    current = (now or _utc_now()).astimezone(dt.timezone.utc)
    if (not values["created_utc"].endswith("Z") or created.tzinfo is None
            or created.astimezone(dt.timezone.utc) > current + dt.timedelta(seconds=5)):
        return {"status": "malformed", "reason": "created_utc_invalid"}
    try:
        current_info = lock.lstat()
    except OSError:
        return {"status": "ambiguous", "reason": "lock_changed_during_inspection"}
    if (current_info.st_dev, current_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
        return {"status": "ambiguous", "reason": "lock_changed_during_inspection"}
    pid = int(values["pid"])
    status, reason = _process_status(pid)
    result: dict[str, object] = {"status": status, "reason": reason, "pid": pid}
    if include_private:
        result["nonce"] = values["nonce"]
        result["created_utc"] = values["created_utc"]
        result["device"] = lock_info.st_dev
        result["inode"] = lock_info.st_ino
    return result


def inspect(root: Path | str) -> dict:
    root_path = _root_path(root)
    with _serialized():
        return _inspect_unlocked(root_path)


def _quarantine(root: Path, *, purpose: str, nonce: str) -> Path:
    if purpose not in {"reclaim", "release"}:
        raise RunLockError("quarantine_purpose_invalid")
    destination = root / f"{LOCK_NAME}.{purpose}-{nonce}"
    _rename_noreplace(root / LOCK_NAME, destination)
    _fsync_directory(root)
    return destination


def _preserve(root: Path) -> Path:
    """Move the current lock aside as PERMANENT evidence.

    Unlike `_quarantine`, nothing ever deletes the result: a lock that had to be moved
    aside is exactly the artifact an operator needs in order to explain a missed session.
    """
    destination = root / f"{LOCK_NAME}.quarantine-{secrets.token_hex(32)}"
    _rename_noreplace(root / LOCK_NAME, destination)
    _fsync_directory(root)
    return destination


def acquire(root: Path | str, *, pid: int,
            now: dt.datetime | None = None) -> dict:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RunLockError("pid_invalid")
    status, _ = _process_status(pid)
    if status != "valid_live":
        raise RunLockError("owner_pid_not_live")
    root_path = _root_path(root)
    nonce = secrets.token_hex(32)
    stage = _make_stage(root_path, pid=pid, nonce=nonce, now=now)
    reclaimed = False
    reclaimed_legacy = False
    evidence: str | None = None
    previous: dict | None = None
    try:
        with _serialized():
            previous = _inspect_unlocked(root_path, now=now, include_private=True)
            if previous["status"] == "legacy_dead":
                preserved = _preserve(root_path)
                try:
                    _rename_noreplace(stage, root_path / LOCK_NAME)
                    _fsync_directory(root_path)
                    reclaimed = True
                    reclaimed_legacy = True
                    evidence = preserved.name
                except Exception:
                    if not (root_path / LOCK_NAME).exists():
                        _rename_noreplace(preserved, root_path / LOCK_NAME)
                        _fsync_directory(root_path)
                    raise
            elif previous["status"] == "valid_dead":
                old_nonce = str(previous["nonce"])
                quarantine = _quarantine(
                    root_path, purpose="reclaim", nonce=old_nonce)
                try:
                    _rename_noreplace(stage, root_path / LOCK_NAME)
                    _fsync_directory(root_path)
                    reclaimed = True
                except Exception:
                    if not (root_path / LOCK_NAME).exists():
                        _rename_noreplace(quarantine, root_path / LOCK_NAME)
                        _fsync_directory(root_path)
                    raise
                _safe_remove_private(quarantine)
            elif previous["status"] == "absent":
                _rename_noreplace(stage, root_path / LOCK_NAME)
                _fsync_directory(root_path)
            else:
                return {
                    "ok": False,
                    "status": str(previous["status"]),
                    "reason": str(previous["reason"]),
                    "pid": previous.get("pid"),
                }
        if reclaimed_legacy:
            reason = "legacy_dead_owner_reclaimed"
        elif reclaimed:
            reason = "dead_owner_reclaimed"
        else:
            reason = "lock_created"
        return {
            "ok": True,
            "status": "acquired",
            "reason": reason,
            "pid": pid,
            "nonce": nonce,
            "reclaimed": reclaimed,
            "reclaimed_legacy": reclaimed_legacy,
            "evidence": evidence,
        }
    finally:
        if stage.exists():
            with contextlib.suppress(Exception):
                _safe_remove_private(stage)


def recover_dead(root: Path | str) -> dict:
    root_path = _root_path(root)
    with _serialized():
        current = _inspect_unlocked(root_path, include_private=True)
        status = str(current["status"])
        if status == "absent":
            return {
                "ok": True, "status": "clear", "reason": "lock_absent",
                "recovered": False,
            }
        if status == "legacy_dead":
            preserved = _preserve(root_path)
            return {
                "ok": True, "status": "recovered",
                "reason": "legacy_dead_owner_reclaimed", "recovered": True,
                "evidence": preserved.name,
            }
        if status != "valid_dead":
            return {
                "ok": False, "status": status, "reason": str(current["reason"]),
                "recovered": False,
            }
        quarantine = _quarantine(
            root_path, purpose="reclaim", nonce=str(current["nonce"]))
        _safe_remove_private(quarantine)
        return {
            "ok": True, "status": "recovered", "reason": "dead_owner_reclaimed",
            "recovered": True,
        }


def quarantine_unsafe(root: Path | str, *, confirm: bool) -> dict:
    """Operator-only escape hatch for a lock that no automated path may touch.

    This exists because the 2026-07-29 outage had NO safe recovery command: the only way
    out of a permanently unsafe lock was `rm -rf`, which destroys the evidence needed to
    explain the missed sessions.  It is never called by a runner and never removes anything.

    It accepts ONLY `legacy_dead` — the single classification in which every historical-shape,
    ownership, mode, age and provably-dead-holder condition has been checked.  `valid_live`,
    `valid_dead`, `malformed`, `symlink` and `ambiguous` are all refused; an unrecognised or
    racing lock is not proven safe to move, and moving it would destroy the forensic state
    needed to explain it.
    """
    root_path = _root_path(root)
    if not confirm:
        return {
            "ok": False, "status": "refused", "reason": "confirmation_required",
            "quarantined": False,
        }
    with _serialized():
        current = _inspect_unlocked(root_path, include_private=True)
        status = str(current["status"])
        if status == "absent":
            return {
                "ok": True, "status": "clear", "reason": "lock_absent",
                "quarantined": False,
            }
        if status not in QUARANTINABLE:
            return {
                "ok": False, "status": status, "reason": str(current["reason"]),
                "quarantined": False,
            }
        preserved = _preserve(root_path)
        return {
            "ok": True, "status": "quarantined", "reason": str(current["reason"]),
            "quarantined": True, "evidence": preserved.name,
        }


def release(root: Path | str, *, pid: int, nonce: str) -> dict:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RunLockError("pid_invalid")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise RunLockError("nonce_invalid")
    root_path = _root_path(root)
    with _serialized():
        current = _inspect_unlocked(root_path, include_private=True)
        if current["status"] == "absent":
            return {
                "ok": True, "status": "absent", "reason": "lock_already_absent",
                "released": False,
            }
        if (current["status"] not in {"valid_live", "valid_dead"}
                or current.get("pid") != pid or current.get("nonce") != nonce):
            return {
                "ok": False, "status": str(current["status"]),
                "reason": "owner_identity_mismatch", "released": False,
            }
        quarantine = _quarantine(root_path, purpose="release", nonce=nonce)
        _safe_remove_private(quarantine)
        return {
            "ok": True, "status": "released", "reason": "owner_identity_matched",
            "released": True,
        }


def _safe_public(result: dict) -> dict:
    return {
        key: value for key, value in result.items()
        if key not in {"nonce", "device", "inode", "created_utc"}
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed repository run-lock helper.")
    parser.add_argument("--root", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    acquire_parser = subparsers.add_parser("acquire")
    acquire_parser.add_argument("--pid", required=True, type=int)
    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--pid", required=True, type=int)
    release_parser.add_argument("--nonce", required=True)
    subparsers.add_parser("inspect")
    subparsers.add_parser("recover-dead")
    quarantine_parser = subparsers.add_parser("quarantine")
    quarantine_parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "acquire":
            result = acquire(args.root, pid=args.pid)
            if result["ok"]:
                print(result["nonce"])
                return 0
            holder = result.get("pid")
            # status:pid:reason — the reason field was added after three consecutive
            # sessions were lost to a bare `status=malformed` that named no cause.
            print("%s:%s:%s" % (
                result["status"],
                holder if isinstance(holder, int) else "?",
                result.get("reason") or "unknown"))
            return RC_CONTENDED
        if args.command == "release":
            result = release(args.root, pid=args.pid, nonce=args.nonce)
            print(json.dumps(_safe_public(result), sort_keys=True))
            return 0 if result["ok"] else RC_CONTENDED
        if args.command == "recover-dead":
            result = recover_dead(args.root)
            print(json.dumps(_safe_public(result), sort_keys=True))
            return 0 if result["ok"] else RC_CONTENDED
        if args.command == "quarantine":
            result = quarantine_unsafe(args.root, confirm=args.confirm)
            print(json.dumps(_safe_public(result), sort_keys=True))
            return 0 if result["ok"] else RC_CONTENDED
        result = inspect(args.root)
        print(json.dumps(_safe_public(result), sort_keys=True))
        return 0
    except (OSError, RunLockError) as exc:
        reason = str(exc).split("\n", 1)[0][:160] or "run_lock_error"
        print(json.dumps({"ok": False, "status": "error", "reason": reason},
                         sort_keys=True))
        return RC_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
