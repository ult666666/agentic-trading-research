#!/usr/bin/env python3
"""Fail-closed SHA-256 for files that carry authority.

WHY
---
`broker_runtime_audit._sha_file` was `path.read_bytes()`: it followed symlinks, had no size cap,
and would happily block forever on a FIFO. The digest still gated authority correctly (a symlink
whose target hashes to the pin IS byte-equivalent), so this was never a privilege bug — but it
was a real availability defect. A PreToolUse hook that hangs on a special file stalls every
audited lane against its 5s timeout, and an unbounded read of a huge file is a trivial local DoS.

CONTRACT
--------
`sha256_file_strict` returns a hex digest ONLY for a real, regular, bounded, readable file, and
returns None for every other outcome. There is no partial success and no exception escapes: a
caller comparing against a pin can therefore treat None as "not the pinned source" and fail
closed without a try/except at every call site.

Rejected: symlinks (at the final component and, where the platform supports it, atomically),
directories, FIFOs, sockets, devices, missing files, unreadable files, empty files, and anything
larger than `max_bytes`.

TOCTOU
------
A pre-open `lstat` alone is advisory — the path can be swapped between the check and the open.
The real guarantee comes from `O_NOFOLLOW` (refuses to open a symlinked final component) plus
`fstat` on the ALREADY-OPEN descriptor: type and size are then read from the same inode that is
subsequently hashed, so a post-open swap cannot change what we measured. Reading proceeds through
that descriptor only, never by re-opening the path.

`O_NOFOLLOW` exists on macOS and Linux. Where it is absent the module falls back to an
lstat-then-fstat consistency check comparing st_dev/st_ino, which closes the same window for
everything except a same-inode race that cannot alter file contents.

Std-lib only. No network. Safe to import from hooks.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

CHUNK_BYTES = 65536
HAS_NOFOLLOW = hasattr(os, "O_NOFOLLOW")


def sha256_file_strict(path: Path | str, max_bytes: int) -> str | None:
    """Hex SHA-256 of a regular, non-symlink, non-empty file of at most `max_bytes`.

    Returns None on ANY rejection or error. Never raises, never returns a partial digest.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        return None
    try:
        target = Path(path)
    except (TypeError, ValueError):
        return None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    # O_NONBLOCK is load-bearing, not decoration. Opening a FIFO O_RDONLY BLOCKS until a writer
    # appears, so without this the "reject non-regular files" check could never be reached — the
    # hook would hang on the open() itself, which is precisely the stall this module exists to
    # prevent. (The first version of this file omitted it and deadlocked its own FIFO test.)
    # On a regular file O_NONBLOCK has no effect.
    flags |= getattr(os, "O_NONBLOCK", 0)
    if HAS_NOFOLLOW:
        flags |= os.O_NOFOLLOW

    fd = None
    try:
        if not HAS_NOFOLLOW:
            # Platform fallback: refuse a symlinked final component up front, then prove after
            # opening that we hold the very inode we inspected.
            pre = os.lstat(target)
            if stat.S_ISLNK(pre.st_mode):
                return None
        fd = os.open(target, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None                      # directory, FIFO, socket, device
        if not HAS_NOFOLLOW and (info.st_dev, info.st_ino) != (pre.st_dev, pre.st_ino):
            return None                      # swapped between lstat and open
        size = info.st_size
        if size < 1 or size > max_bytes:
            return None                      # empty or oversized

        digest = hashlib.sha256()
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(CHUNK_BYTES, remaining))
            if not chunk:
                return None                  # truncated under us -> no authority
            digest.update(chunk)
            remaining -= len(chunk)
        # Anything appended after we measured must not silently extend the digest.
        if os.read(fd, 1):
            return None
        return digest.hexdigest()
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
