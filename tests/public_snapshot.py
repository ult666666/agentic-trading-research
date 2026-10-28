"""Skip helpers for running the legacy tests in the public snapshot.

The public repository is a partial copy of a private one. Some tests import
modules, or read runtime files such as launchd plists, shell runners and
broker policy data, that were deliberately left out. These helpers turn those
cases into skips that name what is missing. A module that IS in this
repository is never skipped: its import error is raised as a real failure.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIRS = ("scripts", "scripts/lib", "scripts/quant", "tests")


def is_published_module(name: str) -> bool:
    top = name.split(".")[0]
    return any((ROOT / folder / f"{top}.py").is_file() or (ROOT / folder / top).is_dir()
               for folder in MODULE_DIRS)


def skip_private_module(exc: ModuleNotFoundError) -> unittest.SkipTest:
    """Turn a missing private module into a module-level skip; re-raise anything else."""
    if not exc.name or is_published_module(exc.name):
        raise exc
    return unittest.SkipTest(
        f"needs {exc.name}, a private module that is not in this public snapshot")


def requires_files(*relative_paths: str):
    """Skip a test or test class unless every listed repository path exists."""
    missing = [path for path in relative_paths if not (ROOT / path).exists()]
    return unittest.skipIf(
        bool(missing),
        f"needs {', '.join(missing)}, private and not in this public snapshot")
